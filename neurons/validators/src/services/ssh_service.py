import hashlib
from base64 import b64encode

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import BestAvailableEncryption


class SSHService:
    def _hash(self, s: bytes) -> bytes:
        return b64encode(hashlib.sha256(s).digest(), altchars=b"-_")

    def _encrypt(self, key: str, payload: str) -> str:
        key_bytes = self._hash(key.encode("utf-8"))
        return Fernet(key_bytes).encrypt(payload.encode("utf-8")).decode("utf-8")

    def generate_ssh_key(self, encryption_key: str) -> (bytes, bytes):
        """Generate SSH key pair.

        Args:
            encryption_key (str): key to encrypt the private key.

        Returns:
            (bytes, bytes): return (private key bytes, public key bytes)
        """
        # Generate a new private-public key pair
        private_key = ed25519.Ed25519PrivateKey.generate()
        public_key = private_key.public_key()

        private_key_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=BestAvailableEncryption(encryption_key.encode()),
        )
        public_key_bytes = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        # extract pub key content, excluding first line and end line
        pub_key_str = "".join(public_key_bytes.decode().split("\n")[1:-2])

        return self._encrypt(encryption_key, private_key_bytes.decode("utf-8")).encode(
            "utf-8"
        ), pub_key_str.encode()
