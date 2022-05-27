from collections import deque

import torch_geometric as pyg
import argparse
import os
import random
import time
from distutils.util import strtobool
from typing import Union
import gym
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.distributions.categorical import Categorical
from torch.utils.tensorboard import SummaryWriter
from torch_geometric.nn import MemPooling
import torch.nn.functional as F
from MathLang import MathLang
from torch_scatter import scatter_mean
from rejoice import envs


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exp-name", type=str, default=os.path.basename(__file__).rstrip(".py"),
                        help="the name of this experiment")
    parser.add_argument("--seed", type=int, default=1,
                        help="seed of the experiment")
    parser.add_argument("--torch-deterministic", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
                        help="if toggled, `torch.backends.cudnn.deterministic=False`")
    parser.add_argument("--cuda", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
                        help="if toggled, cuda will be enabled by default")
    parser.add_argument("--track", type=lambda x: bool(strtobool(x)), default=False, nargs="?", const=True,
                        help="if toggled, this experiment will be tracked with Weights and Biases")
    parser.add_argument("--wandb-project-name", type=str, default="cleanRL",
                        help="the wandb's project name")
    parser.add_argument("--wandb-entity", type=str, default=None,
                        help="the entity (team) of wandb's project")
    parser.add_argument("--capture-video", type=lambda x: bool(strtobool(x)), default=False, nargs="?", const=True,
                        help="weather to capture videos of the agent performances (check out `videos` folder)")
    parser.add_argument("--print-actions", type=bool, default=False,
                        help="print the (action, reward) tuples that make up each episode")

    # Algorithm specific arguments
    parser.add_argument("--env-id", type=str, default="egraph-v0",
                        help="the id of the environment")
    parser.add_argument("--total-timesteps", type=int, default=100_000,
                        help="total timesteps of the experiments")
    parser.add_argument("--learning-rate", type=float, default=2.5e-5,
                        help="the learning rate of the optimizer")
    parser.add_argument("--num-envs", type=int, default=4,
                        help="the number of parallel game environments")
    parser.add_argument("--num-steps", type=int, default=128,
                        help="the number of steps to run in each environment per policy rollout")
    parser.add_argument("--anneal-lr", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
                        help="Toggle learning rate annealing for policy and value networks")
    parser.add_argument("--gae", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
                        help="Use GAE for advantage computation")
    parser.add_argument("--gamma", type=float, default=0.99,
                        help="the discount factor gamma")
    parser.add_argument("--gae-lambda", type=float, default=0.95,
                        help="the lambda for the general advantage estimation")
    parser.add_argument("--num-minibatches", type=int, default=4,
                        help="the number of mini-batches")
    parser.add_argument("--update-epochs", type=int, default=4,
                        help="the K epochs to update the policy")
    parser.add_argument("--norm-adv", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
                        help="Toggles advantages normalization")
    parser.add_argument("--clip-coef", type=float, default=0.2,
                        help="the surrogate clipping coefficient")
    parser.add_argument("--clip-vloss", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
                        help="Toggles whether or not to use a clipped loss for the value function, as per the paper.")
    parser.add_argument("--ent-coef", type=float, default=0.01,
                        help="coefficient of the entropy")
    parser.add_argument("--vf-coef", type=float, default=0.5,
                        help="coefficient of the value function")
    parser.add_argument("--max-grad-norm", type=float, default=0.5,
                        help="the maximum norm for the gradient clipping")
    parser.add_argument("--target-kl", type=float, default=None,
                        help="the target KL divergence threshold")
    args = parser.parse_args()
    args.batch_size = int(args.num_envs * args.num_steps)
    args.minibatch_size = int(args.batch_size // args.num_minibatches)
    return args


def make_env(env_id, seed, idx: int, run_name: str):
    def thunk():
        # TODO: refactor so the lang and expr can be passed in
        lang = MathLang()
        ops = lang.all_operators_obj()
        expr = ops.mul(ops.add(16, 2), ops.mul(4, 0))

        env = gym.make(env_id, lang=lang, expr=expr)
        env = gym.wrappers.TimeLimit(env, max_episode_steps=100)
        env = gym.wrappers.RecordEpisodeStatistics(env)
        env.seed(seed)
        env.action_space.seed(seed)
        return env

    return thunk


def layer_init(layer, std=np.sqrt(2), bias_const=0.0):
    torch.nn.init.orthogonal_(layer.weight, std)
    torch.nn.init.constant_(layer.bias, bias_const)
    return layer


class SAGENetwork(nn.Module):
    def __init__(self, num_node_features: int, n_actions: int, dp_rate_linear=0.2, hidden_size: int = 128,
                 out_std: float = np.sqrt(2)):
        super(SAGENetwork, self).__init__()
        self.gnn = pyg.nn.GraphSAGE(in_channels=num_node_features, hidden_channels=hidden_size,
                                    out_channels=hidden_size, num_layers=2, act="leaky_relu")

        self.mem1 = MemPooling(hidden_size, hidden_size, heads=5, num_clusters=10)
        self.mem2 = MemPooling(hidden_size, n_actions, heads=5, num_clusters=1)

    def forward(self, data: Union[pyg.data.Data, pyg.data.Batch]):
        x = self.gnn(x=data.x, edge_index=data.edge_index)
        x = F.leaky_relu(x)
        x, S1 = self.mem1(x, data.batch)
        x = F.leaky_relu(x)
        x, S2 = self.mem2(x)
        x = x.squeeze(1)
        return x


class GraphTransformerNetwork(nn.Module):
    def __init__(self, num_node_features: int, n_actions: int, dp_rate_linear=0.2, hidden_size: int = 128,
                 out_std: float = np.sqrt(2)):
        super(GraphTransformerNetwork, self).__init__()
        self.gnn = pyg.nn.GraphMultisetTransformer(in_channels=num_node_features, hidden_channels=hidden_size,
                                                   out_channels=n_actions,
                                                   num_heads=4,
                                                   layer_norm=True)

    def forward(self, data: Union[pyg.data.Data, pyg.data.Batch]):
        x = self.gnn(x=data.x, edge_index=data.edge_index, batch=data.batch)
        return x


class PPOAgent(nn.Module):
    def __init__(self, envs: gym.vector.SyncVectorEnv):
        super().__init__()
        self.critic = SAGENetwork(num_node_features=envs.single_observation_space.num_node_features,
                                  n_actions=1,
                                  hidden_size=128,
                                  out_std=1.)
        self.actor = SAGENetwork(num_node_features=envs.single_observation_space.num_node_features,
                                 n_actions=envs.single_action_space.n,
                                 hidden_size=128,
                                 out_std=0.01)  # make probability of each action similar to start with

    def get_value(self, x):
        return self.critic(x)

    def get_action_and_value(self, x, action=None):
        logits = self.actor(x)
        probs = Categorical(logits=logits)
        if action is None:
            action = probs.sample()
        return action, probs.log_prob(action), probs.entropy(), self.critic(x)


def main():
    args = parse_args()
    run_name = f"{args.env_id}__{args.exp_name}__{args.seed}__{int(time.time())}"
    if args.track:
        import wandb
        wandb.init(
            project=args.wandb_project_name,
            entity=args.wandb_entity,
            sync_tensorboard=True,
            config=vars(args),
            name=run_name,
            monitor_gym=True,
            save_code=True,
        )
    writer = SummaryWriter(f"runs/{run_name}")
    writer.add_text(
        "hyperparameters",
        "|param|value|\n|-|-|\n%s" % ("\n".join([f"|{key}|{value}|" for key, value in vars(args).items()])),
    )

    # TRY NOT TO MODIFY: seeding
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.backends.cudnn.deterministic = args.torch_deterministic

    device = torch.device("cuda" if torch.cuda.is_available() and args.cuda else "cpu")

    # env setup
    envs = gym.vector.SyncVectorEnv(
        [make_env(args.env_id, args.seed + i, i, run_name) for i in range(args.num_envs)]
    )
    assert isinstance(envs.single_action_space, gym.spaces.Discrete), "only discrete action space is supported"
    action_names = [r[0] for r in envs.envs[0].lang.all_rules()] + ["end"]

    agent = PPOAgent(envs).to(device)
    optimizer = optim.Adam(agent.parameters(), lr=args.learning_rate, eps=1e-5)

    # ALGO Logic: Storage setup
    # obs = deque(maxlen=args.num_steps)
    obs = np.empty(args.num_steps, dtype=object)

    actions = torch.zeros((args.num_steps, args.num_envs) + envs.single_action_space.shape).to(device)
    logprobs = torch.zeros((args.num_steps, args.num_envs)).to(device)
    rewards = torch.zeros((args.num_steps, args.num_envs)).to(device)
    dones = torch.zeros((args.num_steps, args.num_envs)).to(device)
    values = torch.zeros((args.num_steps, args.num_envs)).to(device)

    # TRY NOT TO MODIFY: start the game
    global_step = 0
    start_time = time.time()
    # Intial observation
    next_obs = pyg.data.Batch.from_data_list(envs.reset()).to(device)
    next_done = torch.zeros(args.num_envs).to(device)
    num_updates = args.total_timesteps // args.batch_size

    for update in range(1, num_updates + 1):
        # Annealing the learning rate if instructed to do so.
        if args.anneal_lr:
            frac = 1.0 - (update - 1.0) / num_updates
            lrnow = frac * args.learning_rate
            optimizer.param_groups[0]["lr"] = lrnow

        # Policy rollout across envs
        for step in range(0, args.num_steps):
            global_step += 1 * args.num_envs
            # add the batch of 4 env observations to the obs list at index step
            obs[step] = next_obs
            # obs.append(next_obs)
            dones[step] = next_done

            # log the action, logprob, and value for this step into our data storage
            with torch.no_grad():  # no grad b/c we're just rolling out, not training
                action, logprob, _, value = agent.get_action_and_value(next_obs)
                values[step] = value.flatten()
            actions[step] = action
            logprobs[step] = logprob

            # execute the chosen action
            next_obs, reward, done, info = envs.step(action.cpu().numpy())
            # log the reward into data storage at this step
            rewards[step] = torch.tensor(reward).to(device).view(-1)
            next_done = torch.Tensor(done).to(device)
            # convert next obs to a pytorch geometric batch
            next_obs = pyg.data.Batch.from_data_list(next_obs).to(device)

            for env_ind, item in enumerate(info):
                if "episode" in item.keys():
                    writer.add_scalar("charts/episodic_return", item["episode"]["r"], global_step)
                    writer.add_scalar("charts/episodic_length", item["episode"]["l"], global_step)
                    writer.add_scalar("charts/episodic_cost", item["actual_cost"], global_step)
                    print(f"global_step={global_step}, episode_length={item['episode']['l']}, episodic_return={item['episode']['r']:.2f}, episodic_cost={item['actual_cost']}")
                    if args.print_actions:
                        start = (step+1) - item["episode"]["l"]
                        if start < 0:
                            # start of episode is at end of buffer
                            start_before = args.num_steps + start
                            ep_actions = [action_names[int(i)] for i in torch.cat([actions[start_before:][:, env_ind], actions[0:step+1][:, env_ind]])]
                            ep_rewards = [x.item() for x in list(torch.cat([rewards[start_before:][:, env_ind], rewards[0:step+1][:, env_ind]]))]
                        else:
                            ep_actions = [action_names[int(i)] for i in actions[start:step+1][:, env_ind]]
                            ep_rewards = [x.item() for x in list(rewards[start:step+1][:, env_ind])]
                        ep_a_r = list(zip(ep_actions, ep_rewards))
                        print(ep_a_r)
                    break
        # Remember that actions is (n_steps, n_envs, n_actions)
        # bootstrap value if not done
        with torch.no_grad():
            next_value = agent.get_value(next_obs).reshape(1, -1)
            if args.gae:
                advantages = torch.zeros_like(rewards).to(device)
                lastgaelam = 0
                for t in reversed(range(args.num_steps)):
                    if t == args.num_steps - 1:
                        nextnonterminal = 1.0 - next_done
                        nextvalues = next_value
                    else:
                        nextnonterminal = 1.0 - dones[t + 1]
                        nextvalues = values[t + 1]
                    delta = rewards[t] + args.gamma * nextvalues * nextnonterminal - values[t]
                    advantages[t] = lastgaelam = delta + args.gamma * args.gae_lambda * nextnonterminal * lastgaelam
                returns = advantages + values
            else:
                returns = torch.zeros_like(rewards).to(device)
                for t in reversed(range(args.num_steps)):
                    if t == args.num_steps - 1:
                        nextnonterminal = 1.0 - next_done
                        next_return = next_value
                    else:
                        nextnonterminal = 1.0 - dones[t + 1]
                        next_return = returns[t + 1]
                    returns[t] = rewards[t] + args.gamma * nextnonterminal * next_return
                advantages = returns - values

        # flatten the batch
        all_obs_raw = []
        for step_batch in obs:
            # TODO: this is a performance hog. Find a diff way?
            all_obs_raw += step_batch.to_data_list()
        b_obs = pyg.data.Batch.from_data_list(all_obs_raw)

        b_logprobs = logprobs.reshape(-1)
        b_actions = actions.reshape((-1,) + envs.single_action_space.shape)
        b_advantages = advantages.reshape(-1)
        b_returns = returns.reshape(-1)
        b_values = values.reshape(-1)

        # Optimizing the policy and value network (learn!)
        b_inds = np.arange(args.batch_size)
        clipfracs = []
        for epoch in range(args.update_epochs):  # update_epochs num of gradient updates
            np.random.shuffle(b_inds)
            for start in range(0, args.batch_size, args.minibatch_size):
                end = start + args.minibatch_size
                mb_inds = b_inds[start:end]

                mb_batch_obs = pyg.data.Batch.from_data_list(b_obs[mb_inds])

                _, newlogprob, entropy, newvalue = agent.get_action_and_value(mb_batch_obs, b_actions.long()[mb_inds])
                logratio = newlogprob - b_logprobs[mb_inds]
                ratio = logratio.exp()

                with torch.no_grad():
                    # calculate approx_kl http://joschu.net/blog/kl-approx.html
                    old_approx_kl = (-logratio).mean()
                    approx_kl = ((ratio - 1) - logratio).mean()
                    clipfracs += [((ratio - 1.0).abs() > args.clip_coef).float().mean().item()]

                mb_advantages = b_advantages[mb_inds]
                if args.norm_adv:
                    mb_advantages = (mb_advantages - mb_advantages.mean()) / (mb_advantages.std() + 1e-8)

                # Policy loss
                pg_loss1 = -mb_advantages * ratio
                pg_loss2 = -mb_advantages * torch.clamp(ratio, 1 - args.clip_coef, 1 + args.clip_coef)
                pg_loss = torch.max(pg_loss1, pg_loss2).mean()

                # Value loss
                newvalue = newvalue.view(-1)
                if args.clip_vloss:
                    v_loss_unclipped = (newvalue - b_returns[mb_inds]) ** 2
                    v_clipped = b_values[mb_inds] + torch.clamp(
                        newvalue - b_values[mb_inds],
                        -args.clip_coef,
                        args.clip_coef,
                    )
                    v_loss_clipped = (v_clipped - b_returns[mb_inds]) ** 2
                    v_loss_max = torch.max(v_loss_unclipped, v_loss_clipped)
                    v_loss = 0.5 * v_loss_max.mean()
                else:
                    v_loss = 0.5 * ((newvalue - b_returns[mb_inds]) ** 2).mean()

                entropy_loss = entropy.mean()
                loss = pg_loss - args.ent_coef * entropy_loss + v_loss * args.vf_coef

                optimizer.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(agent.parameters(), args.max_grad_norm)
                optimizer.step()

            if args.target_kl is not None:
                if approx_kl > args.target_kl:
                    break

        y_pred, y_true = b_values.cpu().numpy(), b_returns.cpu().numpy()
        var_y = np.var(y_true)
        explained_var = np.nan if var_y == 0 else 1 - np.var(y_true - y_pred) / var_y

        # TRY NOT TO MODIFY: record rewards for plotting purposes
        writer.add_scalar("charts/learning_rate", optimizer.param_groups[0]["lr"], global_step)
        writer.add_scalar("losses/value_loss", v_loss.item(), global_step)
        writer.add_scalar("losses/policy_loss", pg_loss.item(), global_step)
        writer.add_scalar("losses/entropy", entropy_loss.item(), global_step)
        writer.add_scalar("losses/old_approx_kl", old_approx_kl.item(), global_step)
        writer.add_scalar("losses/approx_kl", approx_kl.item(), global_step)
        writer.add_scalar("losses/clipfrac", np.mean(clipfracs), global_step)
        writer.add_scalar("losses/explained_variance", explained_var, global_step)
        print("SPS:", int(global_step / (time.time() - start_time)))
        writer.add_scalar("charts/SPS", int(global_step / (time.time() - start_time)), global_step)

    envs.close()
    writer.close()

if __name__ == "__main__":
    main()
