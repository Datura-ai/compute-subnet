import getpass
import os


class MinerSSHService:
    def add_pubkey_to_host(self, pub_key: bytes):
        with open(os.path.expanduser("~/.ssh/authorized_keys"), "a") as file:
            file.write(pub_key.decode() + "\n")

    def get_current_os_user(self) -> str:
        return getpass.getuser()
