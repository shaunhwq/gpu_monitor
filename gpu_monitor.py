import subprocess
from collections import defaultdict
from typing import List, Dict, Tuple
import xml.etree.ElementTree as ET
import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor


def retrieve_ssh_hosts(ssh_config_path: str) -> List[str]:
    """
    Read ssh config file and retrieve all user defined hosts

    E.g.
    Host GPU59
      Hostname XXX.XXX.XX.XX
      Port 10020
      User XXXXXXXX
      ProxyCommand ssh another_host_name nc -w 120 %h %p
      IdentityFile ~/.ssh/id_rsa

    Retrieves 'GPU59' from the above example.

    :param ssh_config_path: Path to ssh config file. E.g. ~/.ssh/config for MacOS users
    :return: List of hosts defined by user
    """
    gpu_strs = []
    with open(ssh_config_path, "r") as f:
        for line in f.readlines():
            if "Host " not in line:
                continue
            gpu_strs.append(line.rstrip().split()[1])

    return gpu_strs


def usernames_from_pids(ssh_host: str, pids: List[str]) -> Tuple[bool, Dict[str, str]]:
    """
    Query ssh host about pid user
    :param ssh_host: Host string defined in ssh config file
    :param pids: List of pid strings to query
    :return: Mapping between pid and process creator username
    """
    base_query_cmd = "ps -o user= -p {}"
    cmd = ["ssh", ssh_host, ";".join([base_query_cmd.format(pid) for pid in pids])]
    ret, users = do_cmd(cmd)
    if not ret:
        return ret, {}
    return ret, {pid: user for pid, user in zip(pids, users.split("\n"))}


def get_host_gpu_info(ssh_host) -> dict:
    """
    Extracts useful information from ssh host
    :param ssh_host: Host string defined in ssh config file
    :return: Dictionary containing useful gpu related information
    """
    # region ssh ssh_host "nvidia-smi -q -x"
    cmd = ["ssh", ssh_host, "nvidia-smi -q -x"]
    ret, xml_str = do_cmd(cmd)

    if not ret:
        return {}
    # endregion

    # region Covert xml to dict
    tree = ET.ElementTree(ET.fromstring(xml_str))
    gpu_dict = etree_to_dict(tree.getroot())["nvidia_smi_log"]
    # endregion

    # region Extract useful information
    host_gpu_usage = {}
    unique_pids = set()

    for gpu_idx in range(len(gpu_dict["gpu"])):
        gpu_info = gpu_dict["gpu"][gpu_idx]
        device = f"cuda:{gpu_info['minor_number']}"

        host_gpu_usage[device] = {
            # "product_name": gpu_dict["product_name"],
            "driver_version": gpu_dict["driver_version"],
            "cuda_version": gpu_dict["cuda_version"],
            "memory": {key: int(value.split()[0]) for (key, value) in gpu_info["fb_memory_usage"].items()},
            "processes": []
        }
        if len(gpu_info["processes"]) == 0:
            continue

        # gpu_info["processes"]["process_info"] is a list if there are a few, if not it is a dictionary
        # len = 1 {"process_info": {}}, len> 1: {"process_info": [{}, ...]}
        if type(gpu_info["processes"]["process_info"]) is dict:
            gpu_info["processes"]["process_info"] = [gpu_info["processes"]["process_info"]]

        for process_infos in gpu_info["processes"].values():
            for process_info in process_infos:
                unique_pids.add(process_info["pid"])
                host_gpu_usage[device]["processes"].append({
                    "pid": process_info["pid"],
                    "used_memory": int(process_info["used_memory"].split()[0])
                })

    if len(unique_pids) == 0:
        return host_gpu_usage

    ret, pid_user_dict = usernames_from_pids(ssh_host, list(unique_pids))
    if not ret:
        return host_gpu_usage

    for device in host_gpu_usage:
        for process in host_gpu_usage[device]["processes"]:
            process["user"] = pid_user_dict[process["pid"]]
    # endregion

    return host_gpu_usage


def etree_to_dict(t) -> dict:
    """
    Converts a xml tree to dictionary
    :param t: XML Element Tree
    :return: Dictionary form of the tree
    """
    d = {t.tag: {} if t.attrib else None}
    children = list(t)
    if children:
        dd = defaultdict(list)
        for dc in map(etree_to_dict, children):
            for k, v in dc.items():
                dd[k].append(v)
        d = {t.tag: {k: v[0] if len(v) == 1 else v
                     for k, v in dd.items()}}
    if t.attrib:
        d[t.tag].update(('@' + k, v)
                        for k, v in t.attrib.items())
    if t.text:
        text = t.text.strip()
        if children or t.attrib:
            if text:
                d[t.tag]['#text'] = text
        else:
            d[t.tag] = text
    return d


def do_cmd(cmd: List[str], timeout: int = 2) -> Tuple[bool, str]:
    """
    Execute command with subprocess.
    :param cmd: Command for subprocess check_output
    :param timeout: Duration to wait before cancelling subprocess command
    :return: (True, stdout_string) if successful, (False, "") if unsuccessful
    """
    ret, output = True, b''
    try:
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.CalledProcessError:
        ret = False
    except subprocess.TimeoutExpired:
        ret = False
    return ret, output.decode()


def print_simple_output(ssh_hosts: List[str], hosts_gpu_info: List[dict], column_width: int = 20) -> None:
    """
    Prints out memory information retrieved from the various hosts in a table format
    :param ssh_hosts: List of host strings extracted from ssh config file
    :param hosts_gpu_info: Useful gpu related information pulled from ssh hosts
    :param column_width: Column width for table
    :return: None
    """
    # Display gpu usage visually
    max_num_gpu = max([len(result) for result in hosts_gpu_info])

    lines = [["Hosts", f"cuda:0 -> cuda:{max_num_gpu - 1}..."]]
    for host, result in zip(ssh_hosts, hosts_gpu_info):
        line1 = [host]
        for i in range(max_num_gpu):
            key = f"cuda:{i}"
            if key not in result:
                continue

            percentage_used = result[key]["memory"]["used"] / result[key]["memory"]["total"]
            used_str = '\33[31m' + "#" * int(round(percentage_used * 10)) + '\33[0m'
            unused_str = '\33[32m' + "-" * int(round((1 - percentage_used) * 10)) + '\33[0m'
            line1.append(f"|{used_str}{unused_str}|")
        lines.append(line1)

    host_formatting_str = "{:<" + str(column_width) + "}"
    for line in lines:
        output_string = host_formatting_str.format(line[0]) + "".join(line[1:])
        print(output_string)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--ssh_config", help="Path to ssh config file", default="~/.ssh/config")
    parser.add_argument("--max_workers", help="Maximum number of workers to use", default=4, type=int)
    args = parser.parse_args()

    ssh_config_file = args.ssh_config
    if sys.platform == "darwin":
        ssh_config_file = os.path.expanduser(args.ssh_config)

    hosts = retrieve_ssh_hosts(ssh_config_file)

    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        results = list(executor.map(get_host_gpu_info, hosts))

    print_simple_output(hosts, results)
