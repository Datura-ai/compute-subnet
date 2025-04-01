import time
import random
import logging
import json 
import os
import uuid as uuid4
import base64
from dataclasses import dataclass
from typing import Self, Callable
from core.utils import _m, get_extra_info
from ctypes import CDLL, c_longlong, POINTER, c_int, c_void_p, c_char_p

logger = logging.getLogger(__name__)


class DMCompVerifyWrapper:
    def __init__(self, lib_name: str):
        """
        Constructor, differentiate miner vs validator libs.
        """
        self._initialized = False
        lib_path = os.path.join(os.path.dirname(__file__), lib_name)
        self._lib = CDLL(lib_path)
        self._setup_lib_functions()

    def _setup_lib_functions(self):
        """
        Set up function signatures for the library.
        """
        # Set up function signatures for the library.
        self._lib.DMCompVerify_new.argtypes = [c_longlong, c_longlong]  # Parameters (long m_dim_n, long m_dim_k)
        self._lib.DMCompVerify_new.restype = POINTER(c_void_p)  # Return type is a pointer to a structure.

        self._lib.generateChallenge.argtypes = [POINTER(c_void_p), c_longlong, c_char_p, c_char_p]
        self._lib.generateChallenge.restype = None

        self._lib.processChallengeResult.argtypes = [POINTER(c_void_p), c_longlong, c_char_p]
        self._lib.processChallengeResult.restype = c_char_p

        self._lib.getUUID.argtypes = [c_void_p]
        self._lib.getUUID.restype = c_char_p

        self._lib.getCipherText.argtypes = [c_void_p]
        self._lib.getCipherText.restype = c_char_p

        self._lib.free.argtypes = [c_void_p]
        self._lib.free.restype = None

        self._initialized = True

    def DMCompVerify_new(self, m_dim_n: int, m_dim_k: int):
        """
        Wrap the C++ function DMCompVerify_new.
        Creates a new DMCompVerify object in C++.
        """
        return self._lib.DMCompVerify_new(m_dim_n, m_dim_k)

    def generateChallenge(self, verifier_ptr: POINTER(c_void_p), seed: int, machine_info: str, uuid: str):
        """
        Wrap the C++ function generateChallenge.
        Generates a challenge using the provided DMCompVerify pointer.
        """
        machine_info_bytes = machine_info.encode('utf-8')
        uuid_bytes = uuid.encode('utf-8')
        self._lib.generateChallenge(verifier_ptr, seed, machine_info_bytes, uuid_bytes)

    def processChallengeResult(self, verifier_ptr: POINTER(c_void_p), seed: int, cipher_text: str) -> int:
        """
        Wrap the C++ function processChallengeResult.
        Processes the challenge result using the provided DMCompVerify pointer.
        """
        result = self._lib.processChallengeResult(verifier_ptr, seed, cipher_text)
        return result

    def getCipherText(self, verifier_ptr: POINTER(c_void_p)) -> str:
        """
        Wrap the C++ function getCipherText.
        Retrieves the cipher text as a string.
        """
        # Fetch the cipher text from the C++ function (assumes it's returned as a char*).
        cipher_text_ptr = self._lib.getCipherText(verifier_ptr)

        if cipher_text_ptr:
            cipher_text = c_char_p(cipher_text_ptr).value  # Decode the C string
            return cipher_text
        else:
            return None

    def getUUID(self, verifier_ptr: POINTER(c_void_p)) -> str:
        """
        Wrap the C++ function getUUID.
        Retrieves the UUID as a string.
        """
         # Extract the pointer returned by the C++ function, and convert it to a C string (char*) using c_char_p
        cipher_text_ptr = self._lib.getUUID(verifier_ptr)

        if cipher_text_ptr:
            cipher_text = c_char_p(cipher_text_ptr).value  # Decode the C string
            return cipher_text
        else:
            return None

    def free(self, ptr: c_void_p):
        """
        Frees memory allocated for the given pointer.
        """
        self._lib.free(ptr)

def encrypt_challenge(m_dim_n, m_dim_k, seed, machine_info, uuid):
    try:
        # Example of usage:
        wrapper = DMCompVerifyWrapper("/usr/lib/libdmcompverify.so")

        # Create a new DMCompVerify object
        verifier_ptr = wrapper.DMCompVerify_new(m_dim_n, m_dim_k)

        print("machine_info:", machine_info)
        wrapper.generateChallenge(verifier_ptr, seed, machine_info, uuid)

        cipher_text = wrapper.getCipherText(verifier_ptr)
        base64_string = base64.b64encode(cipher_text).decode()
        
        return base64_string
    except Exception as e:
        logger.error("Failed encrypt challenge request: %s", str(e))
        return ""


@dataclass
class VerifierParams:
    def __init__(self, dim_n: int = 1000, dim_k: int = 10000, seed: int = 0, uuid: str = ""):
        self.dim_n = dim_n
        self.dim_k = dim_k
        self.seed = seed
        self.uuid = uuid
        self.cipher_text = ""

    @classmethod
    def generate(cls) -> Self:
        # You can modify the range for more randomness or based on specific needs
        dim_n = random.randint(1900, 2000)  # Random dim_n between 1900 and 2000
        dim_k = random.randint(2000000, 2586932)  # Random dim_k between 2000000 and 2586932
        seed = int(time.time())
        uuid = str(uuid4.uuid4())

        return cls(dim_n=dim_n, dim_k=dim_k, seed=seed, uuid=uuid)

    def __str__(self) -> str:
        return f"--dim_n {self.dim_n} --dim_k {self.dim_k} --seed {self.seed} --cipher_text {self.cipher_text}"


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
        executor_info,
        default_extra: dict,
        machine_spec: dict,
    ) -> bool:
        script_path = f"{executor_info.root_dir}/src/decrypt_challenge.py"

        gpu_model = ""
        if machine_spec.get("gpu", {}).get("count", 0) > 0:
            details = machine_spec["gpu"].get("details", [])
            if len(details) > 0:
                gpu_model = details[0].get("name", "")

        gpu_details = machine_spec.get("gpu", {}).get("details", [])
        gpu_count = machine_spec.get("gpu", {}).get("count", 0)
        gpu_uuids = ','.join([detail.get('uuid', '') for detail in gpu_details])

        gpu_info = {"uuids": gpu_uuids, "gpu_count": gpu_count, "gpu_model": gpu_model}
        machine_info = json.dumps(gpu_info, sort_keys=True)

        verifier_params = VerifierParams.generate()
        gpu_memory = self.get_gpu_memory(machine_spec)
        verifier_params.dim_k = int(self.get_max_matrix_dimensions(gpu_memory, verifier_params.dim_n))

        verifier_params.cipher_text = encrypt_challenge(verifier_params.dim_n, verifier_params.dim_k, verifier_params.seed, machine_info, verifier_params.uuid)

        command = (
            f"{executor_info.python_path} {script_path} {verifier_params}"
        )

        log_extra = {
            **default_extra,
            "dim_n": verifier_params.dim_n,
            "dim_k": verifier_params.dim_k,
            "seed": verifier_params.seed,
            "uuid": verifier_params.uuid,
            "cipher_text": verifier_params.cipher_text,
        }

        logger.info(_m("Matrix Multiplication Python Script Command", extra=get_extra_info(log_extra)))
        # Run the script
        result = await ssh_client.run(command)
        logger.info(f"{script_path}: {result}")

        if result is None:
            logger.warning(_m("GPU model validation job failed", extra=get_extra_info(log_extra)))
            return False

        uuid = result.stdout.strip()

        if uuid == verifier_params.uuid:
            logger.info(_m("Matrix Mulitiplication Verification Succeed", extra=get_extra_info(log_extra)))
            return True
        else:
            logger.info(_m("Matrix Mulitiplication Verification Failed", extra=get_extra_info(log_extra)))
            return False
