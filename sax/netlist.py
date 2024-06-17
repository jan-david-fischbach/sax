""" SAX netlist parsing and utilities """

from __future__ import annotations

import os
import re
import warnings
from copy import deepcopy
from functools import lru_cache
from typing import Any, Literal, TypedDict

import black
import networkx as nx
import numpy as np
import yaml
from natsort import natsorted
from pydantic import BaseModel as _BaseModel
from pydantic import BeforeValidator, ConfigDict, Field, RootModel, field_validator
from typing_extensions import Annotated

from .utils import clean_string, hash_dict


class NetlistDict(TypedDict):
    instances: dict
    connections: dict[str, str]
    ports: dict[str, str]


RecursiveNetlistDict = dict[str, NetlistDict]


class BaseModel(_BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        json_encoders={np.ndarray: lambda arr: np.round(arr, 12).tolist()},
    )

    def __repr__(self):
        s = super().__repr__()
        s = black.format_str(s, mode=black.Mode())
        return s

    def __str__(self):
        return self.__repr__()

    def __hash__(self):
        return hash_dict(self.model_dump())


class Component(BaseModel):
    component: str
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("component")
    def validate_component_name(cls, value):
        if "," in value:
            raise ValueError(
                f"Invalid component string. Should not contain ','. Got: {value}"
            )
        value = value.split("$")[0]
        return clean_string(value)


PortPlacement = Literal["ce", "cw", "nc", "ne", "nw", "sc", "se", "sw", "cc", "center"]


class Placement(BaseModel):
    x: str | float = 0.0
    y: str | float = 0.0
    dx: str | float = 0.0
    dy: str | float = 0.0
    rotation: float = 0.0
    mirror: bool = False
    xmin: str | float | None = None
    xmax: str | float | None = None
    ymin: str | float | None = None
    ymax: str | float | None = None
    port: str | PortPlacement | None = None


def _str_to_component(s: Any) -> Component:
    if isinstance(s, str):
        return Component(component=s)
    return Component.model_validate(s)


CoercingComponent = Annotated[Component | str, BeforeValidator(_str_to_component)]


class Netlist(BaseModel):
    instances: dict[str, CoercingComponent] = Field(default_factory=dict)
    connections: dict[str, str] = Field(default_factory=dict)
    ports: dict[str, str] = Field(default_factory=dict)
    placements: dict[str, Placement] = Field(default_factory=dict)

    @staticmethod
    def clean_instance_string(value):
        if "," in value:
            raise ValueError(
                f"Invalid instance string. Should not contain ','. Got: {value}"
            )
        return clean_string(value)

    @field_validator("instances")
    def validate_instance_names(cls, instances):
        return {cls.clean_instance_string(k): v for k, v in instances.items()}

    @field_validator("placements")
    def validate_placement_names(cls, placements):
        if placements is not None:
            return {cls.clean_instance_string(k): v for k, v in placements.items()}
        return {}

    @classmethod
    def clean_connection_string(cls, value):
        *comp, port = value.split(",")
        comp = cls.clean_instance_string(",".join(comp))
        return f"{comp},{port}"

    @field_validator("connections")
    def validate_connection_names(cls, connections):
        return {
            cls.clean_connection_string(k): cls.clean_connection_string(v)
            for k, v in connections.items()
        }

    @field_validator("ports")
    def validate_port_names(cls, ports):
        return {
            cls.clean_instance_string(k): cls.clean_connection_string(v)
            for k, v in ports.items()
        }


class RecursiveNetlist(RootModel):
    root: dict[str, Netlist]

    model_config = ConfigDict(
        frozen=True,
        json_encoders={np.ndarray: lambda arr: np.round(arr, 12).tolist()},
    )

    def __repr__(self):
        s = super().__repr__()
        s = black.format_str(s, mode=black.Mode())
        return s

    def __str__(self):
        return self.__repr__()

    def __hash__(self):
        return hash_dict(self.model_dump())


AnyNetlist = Netlist | NetlistDict | RecursiveNetlist | RecursiveNetlistDict


def netlist(netlist: AnyNetlist, remove_unused_instances=False) -> RecursiveNetlist:
    """return a netlist from a given dictionary"""
    if isinstance(netlist, RecursiveNetlist):
        net = netlist
    elif isinstance(netlist, Netlist):
        net = RecursiveNetlist(root={"top_level": netlist})
    elif isinstance(netlist, dict):
        if "instances" in netlist:
            flat_net = Netlist.model_validate(netlist)
            net = RecursiveNetlist.model_validate({"top_level": flat_net})
        else:
            net = RecursiveNetlist.model_validate(netlist)
    else:
        raise ValueError(
            "Invalid argument for `netlist`. "
            "Expected type: dict | Netlist | RecursiveNetlist. "
            f"Got: {type(netlist)}."
        )
    if remove_unused_instances:
        recnet_dict: RecursiveNetlistDict = _remove_unused_instances(net.model_dump())
        net = RecursiveNetlist.model_validate(recnet_dict)
    return net


def flatten_netlist(recnet: RecursiveNetlistDict, sep: str = "~"):
    first_name = list(recnet.keys())[0]
    net = _copy_netlist(recnet[first_name])
    _flatten_netlist(recnet, net, sep)
    return net


@lru_cache()
def load_netlist(pic_path: str) -> Netlist:
    with open(pic_path, "r") as file:
        net = yaml.safe_load(file.read())
    return Netlist.model_validate(net)


@lru_cache()
def load_recursive_netlist(pic_path: str, ext: str = ".yml"):
    folder_path = os.path.dirname(os.path.abspath(pic_path))

    def _clean_string(path: str) -> str:
        return clean_string(re.sub(ext, "", os.path.split(path)[-1]))

    # the circuit we're interested in should come first:
    netlists: dict[str, Netlist] = {_clean_string(pic_path): Netlist()}

    for filename in os.listdir(folder_path):
        path = os.path.join(folder_path, filename)
        if not os.path.isfile(path) or not path.endswith(ext):
            continue
        netlists[_clean_string(path)] = load_netlist(path)

    return RecursiveNetlist.model_validate(netlists)


def get_netlist_instances_by_prefix(
    recursive_netlist: RecursiveNetlist,
    prefix: str,
):
    """
    Returns a list of all instances with a given prefix in a recursive netlist.

    Args:
        recursive_netlist: The recursive netlist to search.
        prefix: The prefix to search for.

    Returns:
        A list of all instances with the given prefix.
    """
    recursive_netlist_root = recursive_netlist.model_dump()
    result = []
    for key in recursive_netlist_root.keys():
        if key.startswith(prefix):
            result.append(key)
    return result


def get_component_instances(
    recursive_netlist: RecursiveNetlist,
    top_level_prefix: str,
    component_name_prefix: str,
):
    """
    Returns a dictionary of all instances of a given component in a recursive netlist.

    Args:
        recursive_netlist: The recursive netlist to search.
        top_level_prefix: The prefix of the top level instance.
        component_name_prefix: The name of the component to search for.

    Returns:
        A dictionary of all instances of the given component.
    """
    instance_names = []
    recursive_netlist_root = recursive_netlist.model_dump()

    # Should only be one in a netlist-to-digraph. Can always be very specified.
    top_level_prefixes = get_netlist_instances_by_prefix(
        recursive_netlist, prefix=top_level_prefix
    )
    top_level_prefix = top_level_prefixes[0]
    for key in recursive_netlist_root[top_level_prefix]["instances"]:
        if recursive_netlist_root[top_level_prefix]["instances"][key][
            "component"
        ].startswith(component_name_prefix):
            # Note priority encoding on match.
            instance_names.append(key)
    return {component_name_prefix: instance_names}


def _remove_unused_instances(recursive_netlist: RecursiveNetlistDict):
    recursive_netlist = {**recursive_netlist}

    for name, flat_netlist in recursive_netlist.items():
        recursive_netlist[name] = _remove_unused_instances_flat(flat_netlist)

    return recursive_netlist


def _get_connectivity_netlist(netlist):
    connectivity_netlist = {
        "instances": natsorted(netlist["instances"]),
        "connections": [
            (c1.split(",")[0], c2.split(",")[0])
            for c1, c2 in netlist["connections"].items()
        ],
        "ports": [(p, c.split(",")[0]) for p, c in netlist["ports"].items()],
    }
    return connectivity_netlist


def _get_connectivity_graph(netlist):
    graph = nx.Graph()
    connectivity_netlist = _get_connectivity_netlist(netlist)
    for name in connectivity_netlist["instances"]:
        graph.add_node(name)
    for c1, c2 in connectivity_netlist["connections"]:
        graph.add_edge(c1, c2)
    for c1, c2 in connectivity_netlist["ports"]:
        graph.add_edge(c1, c2)
    return graph


def _get_nodes_to_remove(graph, netlist):
    nodes = set()
    for port in netlist["ports"]:
        nodes |= nx.descendants(graph, port)
    nodes_to_remove = set(graph.nodes) - nodes
    return list(nodes_to_remove)


def _remove_unused_instances_flat(flat_netlist: NetlistDict) -> NetlistDict:
    flat_netlist = {**flat_netlist}

    flat_netlist["instances"] = {**flat_netlist["instances"]}
    flat_netlist["connections"] = {**flat_netlist["connections"]}
    flat_netlist["ports"] = {**flat_netlist["ports"]}

    graph = _get_connectivity_graph(flat_netlist)
    names = set(_get_nodes_to_remove(graph, flat_netlist))

    for name in list(names):
        if name in flat_netlist["instances"]:
            del flat_netlist["instances"][name]

    for conn1, conn2 in list(flat_netlist["connections"].items()):
        for conn in [conn1, conn2]:
            name, _ = conn.split(",")
            if name in names and conn1 in flat_netlist["connections"]:
                del flat_netlist["connections"][conn1]

    for pname, conn in list(flat_netlist["ports"].items()):
        name, _ = conn.split(",")
        if name in names and pname in flat_netlist["ports"]:
            del flat_netlist["ports"][pname]

    return flat_netlist


def _copy_netlist(net):
    net = {
        k: deepcopy(v)
        for k, v in net.items()
        if k in ["instances", "connections", "ports"]
    }
    return net


def _flatten_netlist(recnet, net, sep):
    for name, instance in list(net["instances"].items()):
        component = instance["component"]
        if component not in recnet:
            continue
        del net["instances"][name]
        child_net = _copy_netlist(recnet[component])
        _flatten_netlist(recnet, child_net, sep)
        for iname, iinstance in child_net["instances"].items():
            net["instances"][f"{name}{sep}{iname}"] = iinstance
        ports = {k: f"{name}{sep}{v}" for k, v in child_net["ports"].items()}
        for ip1, ip2 in list(net["connections"].items()):
            n1, p1 = ip1.split(",")
            n2, p2 = ip2.split(",")
            if n1 == name:
                del net["connections"][ip1]
                if p1 not in ports:
                    warnings.warn(
                        f"Port {ip1} not found. Connection {ip1}<->{ip2} ignored."
                    )
                    continue
                net["connections"][ports[p1]] = ip2
            elif n2 == name:
                if p2 not in ports:
                    warnings.warn(
                        f"Port {ip2} not found. Connection {ip1}<->{ip2} ignored."
                    )
                    continue
                net["connections"][ip1] = ports[p2]
        for ip1, ip2 in child_net["connections"].items():
            net["connections"][f"{name}{sep}{ip1}"] = f"{name}{sep}{ip2}"
        for p, ip2 in list(net["ports"].items()):
            try:
                n2, p2 = ip2.split(",")
            except ValueError:
                warnings.warn(f"Unconventional port definition ignored: {p}->{ip2}.")
                continue
            if n2 == name:
                if p2 in ports:
                    net["ports"][p] = ports[p2]
                else:
                    del net["ports"][p]
