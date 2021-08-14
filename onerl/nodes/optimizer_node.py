import pickle
import multiprocessing as mp
import ctypes
import os
import time

import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel

from onerl.utils.import_module import get_class_from_str
from onerl.utils.shared_state_dict import SharedStateDict
from onerl.utils.batch.cuda import BatchCuda
from onerl.nodes.node import Node


class OptimizerNode(Node):
    @staticmethod
    def create_algo(ns_config, ddp_device=None):
        algo_config = ns_config["algorithm"]
        # create network
        network = {k: get_class_from_str(v.get("import", ""), v["name"])(**v.get("params", {}))
                   for k, v in algo_config.get("network", {}).items()}
        if ddp_device is not None:
            network = {k: DistributedDataParallel(v.to(ddp_device), device_ids=[ddp_device])
                       for k, v in network.items()}

        algo_class = get_class_from_str(algo_config.get("import", ""), algo_config["name"])
        return algo_class(network=network, env_params=ns_config["env"], **algo_config.get("params", {}))

    @staticmethod
    def node_create_shared_objects(node_class: str, num: int, ns_config: dict):
        objects = Node.node_create_shared_objects(node_class, num, ns_config)
        # policy state dict example
        example_policy_state_dict = OptimizerNode.create_algo(ns_config).policy_state_dict()
        # rank 0 only, policy update
        objects[0].update({
            "update_lock": mp.Lock(),
            "update_version": mp.Value(ctypes.c_int64, -1, lock=False),
            "update_state_dict": SharedStateDict(example_policy_state_dict)
        })
        return objects

    def run(self):
        # distributed data parallel (DDP)
        # setup DDP
        # FIXME: Single machine multi-GPU setting
        os.environ["MASTER_ADDR"] = "localhost"
        os.environ["MASTER_PORT"] = self.config.get("port", "12355")
        dist.init_process_group("nccl", rank=self.node_rank,
                                world_size=self.node_count(self.node_class, self.ns_config))
        # allocate device
        devices = self.config["devices"]
        device = torch.device(devices[self.node_rank % len(devices)])
        # model
        algorithm = self.create_algo(self.ns_config, device)
        algorithm.train()

        # updater
        last_update_time = time.time()
        current_model_version = 0

        local_update_state_dict = None
        shared_update_state_dict = None
        if self.node_rank == 0:
            local_update_state_dict = algorithm.policy_state_dict()
            shared_update_state_dict = self.objects["update_state_dict"]
            shared_update_state_dict.start()

        # optimizer
        node_sampler = self.find("SamplerNode", self.node_rank)
        batch = BatchCuda(self.global_objects[node_sampler]["batch"], device)
        # sample first batch
        self.send(node_sampler, "")

        while True:
            # wait & copy batch
            self.setstate("wait")
            batch.wait_ready()
            self.setstate("copy")
            batch.copy_from()
            torch.cuda.synchronize()  # copy is asynchronous
            # notify to sample
            self.send(node_sampler, "")

            # optimize
            self.setstate("step")
            metric = algorithm.learn(batch)
            if metric is not None:
                # update to data logging
                if self.node_rank == 0:
                    metric["update"] = 1

                self.log_metric(metric)

            # update (if needed)
            if self.node_rank == 0:
                current_model_version += 1
                current_time = time.time()
                if (current_time - last_update_time) >= self.config["update_interval"]:
                    last_update_time = current_time

                    # update shared policy (lock free)
                    self.setstate("update_policy")
                    shared_update_state_dict.load_from(local_update_state_dict)

                    self.objects["update_lock"].acquire()
                    self.objects["update_version"].value = current_model_version
                    self.objects["update_lock"].release()
