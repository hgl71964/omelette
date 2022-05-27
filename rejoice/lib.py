from .rejoice import *
from typing import Protocol, Union, NamedTuple
from collections import OrderedDict
import torch
import torch_geometric as geom
import networkx as nx
import matplotlib.pyplot as plt
import numpy as np
import torch_geometric.transforms as T


class ObjectView(object):
    def __init__(self, d):
        self.__dict__ = d


class Language(Protocol):
    """A base Language for an equality saturation task. This will be passed to egg."""

    def op_tuple(self, op):
        """Convert an operator string (name, x, y, ...) into a named tuple"""
        name, *args = op
        tup = NamedTuple(name, [(a, int) for a in args])
        globals()[name] = tup
        return tup

    def eclass_analysis(self, *args) -> any:
        ...

    def all_operators(self) -> list[tuple]:
        ...

    def all_operators_obj(self):
        op_dict = dict([(operator.__name__.lower(), operator) for operator in self.all_operators()])
        return ObjectView(op_dict)

    def all_rules(self) -> list[list]:
        ...

    def rewrite_rules(self):
        rules = list()
        for rl in self.all_rules():
            name = rl[0]
            frm = rl[1]
            to = rl[2]
            rules.append(Rewrite(frm, to, name))
        return rules

    @property
    def num_node_features(self) -> int:
        return 4 + len(self.all_operators())

    def get_feature_upper_bounds(self):
        return np.array([1, 1, 1, np.inf] + ([1] * len(self.all_operators())))

    def encode_node(self, operator: Union[int, tuple]) -> torch.Tensor:
        """[is_eclass, is_enode, is_scalar, scalar_val, ...onehot_optype]"""
        onehot = torch.zeros(self.num_node_features)

        if isinstance(operator, int):
            onehot[2] = 1
            onehot[3] = operator
            return onehot

        # is an enode
        onehot[1] = 1

        for ind, op in enumerate(self.all_operators()):
            if isinstance(operator, op):
                onehot[4 + ind] = 1
                return onehot

        raise Exception("Failed to encode node")

    def feature_names(self):
        features = ["is_eclass",
                    "is_enode",
                    "is_scalar",
                    "scalar_val"]

        op_names = [op.__name__ for op in self.all_operators()]
        return features + op_names

    def decode_node(self, node: torch.Tensor):
        dnode = {"type": "eclass" if node[0] == 1 else "enode", "is_scalar": bool(node[2]), "value": node[3]}
        # if it's an enode, find its op type
        if node[1] == 1:
            all_ops = self.all_operators()
            ind = torch.argmax(torch.Tensor(node[4:])).item()
            op = all_ops[ind]
            dnode["op"] = op.__name__
        return dnode

    def encode_eclass(self, eclass_id: int, data: any) -> torch.Tensor:
        onehot = torch.zeros(self.num_node_features)
        onehot[0] = 1
        return onehot

    def encode_egraph(self, egraph: EGraph) -> geom.data.Data:
        classes = egraph.classes()
        all_nodes = []
        all_edges = []
        edge_attr = []
        eclass_to_ind = {}

        # Insert eclasses first as enodes will refer to them
        for eclass_id, (data, nodes) in classes.items():
            all_nodes.append(self.encode_eclass(eclass_id, data))
            eclass_to_ind[eclass_id] = len(all_nodes) - 1

        for eclass_id, (data, nodes) in classes.items():
            for node in nodes:
                all_nodes.append(self.encode_node(node))

                # connect each node to its eclass
                all_edges.append(torch.Tensor([eclass_to_ind[eclass_id], len(all_nodes) - 1]))
                edge_attr.append(torch.Tensor([0, 1]))

                # connect each node to its child eclasses
                if isinstance(node, tuple):
                    for ecid in node:
                        all_edges.append(torch.Tensor([len(all_nodes) - 1, eclass_to_ind[str(ecid)]]))
                        edge_attr.append(torch.Tensor([1, 0]))

        x = torch.stack(all_nodes, dim=0)
        edge_index = torch.stack(all_edges, dim=0).T.long()
        edge_attr = torch.stack(edge_attr, dim=0)
        edge_index, edge_attr = geom.utils.add_remaining_self_loops(edge_index, edge_attr)
        data = geom.data.Data(x=x, edge_index=edge_index, edge_attr=edge_attr)
        return data

    def viz_egraph(self, data):
        """Vizualize a PyTorch Geometric data object containing an egraph."""
        g = geom.utils.to_networkx(data, node_attrs=['x'])

        for u, data in g.nodes(data=True):
            decoded = self.decode_node(data["x"])
            if decoded["type"] == "eclass":
                data['name'] = "eclass"
            elif decoded["is_scalar"]:
                data['name'] = decoded["value"]
            else:
                data["name"] = decoded["op"]
            del data['x']

        node_labels = {}
        for u, data in g.nodes(data=True):
            node_labels[u] = data['name']

        pos = nx.nx_agraph.graphviz_layout(g, prog="dot")
        nx.draw(g, labels=node_labels, pos=pos)
        plt.imshow()
        return g
