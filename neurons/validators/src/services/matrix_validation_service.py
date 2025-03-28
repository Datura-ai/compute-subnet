import time
import random
import logging

from dataclasses import dataclass
from typing import Self, Callable
from core.utils import _m, get_extra_info
from .const import DATA_CENTER_GPU_MODELS

logger = logging.getLogger(__name__)


@dataclass
class VerifierParams:
    def __init__(self, dim_n: int = 1000, dim_k: int = 10000, seed: int = 0):
        self.dim_n = dim_n
        self.dim_k = dim_k
        self.seed = seed
        self.result_path = "/root/validate_result.txt"

    @classmethod
    def generate(cls) -> Self:
        # You can modify the range for more randomness or based on specific needs
        dim_n = random.randint(1900, 2000)  # Random dim_n between 1900 and 2000
        dim_k = random.randint(2000000, 2586932)  # Random dim_k between 2000000 and 2586932
        seed = int(time.time())

        return cls(dim_n=dim_n, dim_k=dim_k, seed=seed)

    def __str__(self) -> str:
        return f"--dim_n {self.dim_n} --dim_k {self.dim_k} --seed {self.seed} --result_path {self.result_path}"


class H100Prover:
    def __init__(self, dim_n, dim_k, seed, default_extra: dict):
        """Initialize the prover with matrix dimensions and seed."""
        self.dim_n = dim_n
        self.dim_k = dim_k
        self.seed = seed
        self.A = 6364136223846793005
        self.C = 1442695040888963407
        self.M = 9223372036854775807
        self.bandwidth = 0
        self.matrix = []
        self.default_extra = default_extra

    def lcg_rand_host(self, seed):
        """Linear congruential generator to simulate random values."""
        return ((self.A * seed + self.C) % self.M) / self.M

    def matrix_mul(self, row, col):
        """Matrix multiplication optimized by reducing redundant calculations."""
        value = 0.0
        division_factor = 1 if self.dim_n <= 100 and self.dim_k <= 100 else 10

        for i in range(self.dim_k):
            seed_a = self.seed + row * self.dim_k + i
            rand_num_a = self.lcg_rand_host(seed_a) / division_factor

            seed_b = self.seed + i * self.dim_n + col + self.dim_k * self.dim_n
            rand_num_b = self.lcg_rand_host(seed_b) / division_factor

            value += rand_num_a * rand_num_b

        index_factor = (row * self.dim_n + col + value) / (self.dim_n * self.dim_n)
        index_increase = index_factor * self.dim_n * 1.5

        return value + index_increase

    def validate_verification_result(self):
        """Validate the verification result."""

        logger.info(_m("Validation started", extra=get_extra_info(self.default_extra)))

        start_time = time.time()

        # Select a random row/column
        min_dim = min(self.dim_n, self.dim_k)
        row = random.randint(0, min_dim - 1)
        col = random.randint(0, min_dim - 1)
        sum_val = round(self.matrix_mul(row, col), 2)

        logger.info(_m(f"Multiplication value on validator = {sum_val}", extra=get_extra_info(self.default_extra)))
        logger.info(_m(f"Multiplication value from ssh client = {self.matrix[row * self.dim_n + col]}", extra=get_extra_info(self.default_extra)))

        end_time = time.time()
        logger.info(_m(f"Matrix calculation time = {end_time - start_time} seconds", extra=get_extra_info(self.default_extra)))

        if abs(self.matrix[row * self.dim_n + col] - sum_val) < 0.001 * self.dim_n:
            logger.info(_m("Verification Results matches", extra=get_extra_info(self.default_extra)))
        else:
            logger.error(_m("Verification Failed. Result does not match", extra=get_extra_info(self.default_extra)))
            return False

        logger.info(_m("SUCCESS: Validate Passed", extra=get_extra_info(self.default_extra)))

        return True

    def parse_validate_result_content(self, contents):
        """Read and parse the validation result file."""
        try:
            # Read the dimensions DIM_N and DIM_K
            dim_line = contents[0].strip()
            dimensions_str = dim_line.split("Dimension N: ")[1]
            DIM_N_str, DIM_K_str = dimensions_str.split(", K: ")
            # self.dim_n = int(DIM_N_str)
            # self.dim_k = int(DIM_K_str)

            # Read the matrix values
            self.matrix = [
                float(value)
                for line in contents[2: 2 + self.dim_n]
                for value in line.split()
            ]

            # Read the bandwidth
            bandwidth_line = contents[2 + self.dim_n].strip()
            bandwidth = float(bandwidth_line.split(": ")[1])

            self.bandwidth = bandwidth

            default_extra = {
                **self.default_extra,
                "dim_n": self.dim_n,
                "dim_k": self.dim_k,
                "bandwidth": bandwidth
            }

            logger.info(_m("Read successfully the validation result file", extra=get_extra_info(default_extra)))
        except Exception as e:
            logger.error(_m(f"Error reading the file: {e}", extra={}))


class ValidationService:
    def get_gpu_memory(self, machine_spec: dict) -> bool:
        """
        Check if machine has data center GPUs (A100, H100, H200 or similar with >40GB memory)
        A data center GPU, or Graphics Processing Unit, is a specialized electronic circuit that speeds up tasks in data centers. 
        GPUs are used to perform parallel processing, which is ideal for workloads that require simultaneous computations. 
        Args:
            machine_spec: Machine specification dictionary

        Returns:
            bool: is_data_center
        """
        if machine_spec.get("gpu", {}).get("count", 0) > 0:
            details = machine_spec["gpu"].get("details", [])
            if len(details) > 0:
                gpu_model = details[0].get("name", "")
                gpu_memory = details[0].get("capacity", 0)  # Memory in MB

                return gpu_memory

        return 0

    def get_max_matrix_dimensions(self, gpu_memory, dim_n):
        gpu_memory = gpu_memory - 2 * 1024
        max_memory = gpu_memory * (1024.0 ** 2)

        element_size = 8  # 8 bytes for double precision

        # Calculate maximum number of elements that can fit in the available memory
        max_elements = max_memory // element_size

        max_dim_k = max_elements // (2 * dim_n) - dim_n

        return max_dim_k

    async def validate_gpu_model_and_process_job(
        self,
        ssh_client,
        miner_info,
        executor_info,
        remote_dir: str,
        verifier_file_name: str,
        default_extra: dict,
        machine_spec: dict,
        _run_task: Callable
    ) -> bool:

        remote_verifier_file_path = f"{remote_dir}/{verifier_file_name}"
        remote_result_validation_file_path = f"{remote_dir}/validate_result.txt"
        verifier_params = VerifierParams.generate()
        gpu_memory = self.get_gpu_memory(machine_spec)
        verifier_params.dim_k = int(self.get_max_matrix_dimensions(gpu_memory, verifier_params.dim_n))
        verifier_params.result_path = remote_result_validation_file_path

        log_extra = {
            **default_extra,
            "dim_n": verifier_params.dim_n,
            "dim_k": verifier_params.dim_k,
        }

        logger.info(_m("Start matrix job", extra=get_extra_info(log_extra)))

        # Make the remote verifier file executable
        await ssh_client.run(f"chmod +x {remote_verifier_file_path}")
        # Run the verifier command
        verify_results, err = await _run_task(
            ssh_client=ssh_client,
            miner_hotkey=miner_info.miner_hotkey,
            executor_info=executor_info,
            command=f"{remote_verifier_file_path} {verifier_params}",
        )
        if not verify_results:
            logger.warning(_m("GPU model validation job failed", extra=get_extra_info(log_extra)))
            return False

        # Read the result from the validation file
        file_read_results, err = await _run_task(
            ssh_client=ssh_client,
            miner_hotkey=miner_info.miner_hotkey,
            executor_info=executor_info,
            command=f"cat {remote_result_validation_file_path}",
        )
        if not file_read_results:
            logger.warning(_m("No result from GPU model validation job", extra=get_extra_info(log_extra)))
            return False
        # Validate the verification result
        prover = H100Prover(verifier_params.dim_n, verifier_params.dim_k, verifier_params.seed, default_extra)
        prover.parse_validate_result_content(file_read_results)
        is_valid = prover.validate_verification_result()
        return is_valid
