from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import BestAvailableEncryption


class SSHService:
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

        return private_key_bytes, public_key_bytes
