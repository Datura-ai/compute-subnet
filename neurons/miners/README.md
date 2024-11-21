# Miner

## Overview

This miner allows you to contribute your GPU resources to the Compute Subnet and earn compensation for providing computational power. You will run a central miner on a CPU server, which manages multiple executors running on GPU-equipped machines.

### Central Miner Server Requirements

To run the central miner, you only need a CPU server (hardware or Virtual Machine) with the following specifications:

- **CPU**: 4 cores
- **RAM**: 8GB
- **Storage**: 50GB available disk space
- **OS**: Ubuntu (recommended)

### Executors

Executors are GPU-equipped machines that perform the computational tasks. The central miner manages these executors, which can be easily added or removed from the network.

To see the compatible GPUs to mine with and their relative rewards, see this dict [here](https://github.com/Datura-ai/compute-subnet/blob/main/neurons/validators/src/services/const.py#L3).

## Installation

### Using Docker

#### Step 1: Install Docker

Add Docker's official GPG key
```
sudo apt-get update
sudo apt-get install ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
```
Add the repository to Apt sources
```
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update

sudo apt-get install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

test Docker install
```
sudo docker run hello-world
```

add user to docker group
```
sudo groupadd docker
sudo usermod -aG docker $USER
```

Logout and login 

#### Step 2: Clone the Git Repository

```
git clone https://github.com/Datura-ai/compute-subnet.git
```

#### Step 3: Install Required Tools

```
cd compute-subnet && chmod +x scripts/install_miner_on_ubuntu.sh && ./scripts/install_miner_on_ubuntu.sh
```

#### Step 4: Install Bittensor
```
python3 -m venv bt_venv
source bt_venv/bin/activate
pip install bittensor
```

#### Step 5: Setup Env
```
cp neurons/miners/.env.template neurons/miners/.env
```
.env file:
```
BITTENSOR_WALLET_NAME=your_coldkey_name
BITTENSOR_WALLET_HOTKEY_NAME=your_hotkey_name

POSTGRES_DB=compute-subnet-db
POSTGRES_PORT=7432
POSTGRES_USER=postgres
POSTGRES_PASSWORD=password
SQLALCHEMY_DATABASE_URI=postgresql://postgres:password@localhost:7432/compute-subnet-db

BITTENSOR_NETUID=51
BITTENSOR_NETWORK=finney

EXTERNAL_IP_ADDRESS=your_public_ip_address  # pro tip: use `curl ifconfig.me` to find this out
INTERNAL_PORT=any_port_not_already_in_use
EXTERNAL_PORT=any_port_not _already_in_use  # make sure this port is open to external connections

HOST_WALLET_DIR=path_to_where_your_/.bittensor/wallets_is_installed
```

Verify if bittensor and docker installed: 
```
btcli --version
```

```
docker --version
```

#### Step 6: Start the Miner
!Make sure your VENV is activated! (`source ~/bt_venv/bin/activate`)
```
cd neurons/miners && docker compose up -d
```

## Managing Executors

### Adding an Executor

Executors are machines running on GPUs that you can add to your central miner. The more executors (GPUs) you have, the greater your compensation will be. Here's how to add them:

1. Ensure the executor machine is set up and running Docker. For more information, follow the [executor README.md here](../executor/README.md)
2. Register to the subnet before adding the executor to the miner.
3. Use the following command to add an executor to the central miner:

    ```bash
    docker exec <container-id or name> python /root/app/src/cli.py add-executor --address <your-executor-ip-address> --port <your-executor-port> --validator <validator-hotkey-selected-from-list>
    ```

    - `<executor-ip-address>`: The IP address of the executor machine.
    - `<executor-port>`: The port number used for the executor (default: `8001`).
    - `<validator-hotkey>`: The validator hotkey that you want to give access to this executor. Which validator hotkey should you pick? Follow [this guide](assigning_validator_hotkeys.md)

### What is a Validator Hotkey?

The **validator hotkey** is a unique identifier tied to a validator that authenticates and verifies the performance of your executor machines. When you specify a validator hotkey during executor registration, it ensures that your executor is validated by this specific validator.

To switch to a different validator first follow the instructions for removing an executor. After removing the executor, you need to re-register executors by running the add-executor command again (Step 2 of Adding an Executor).

### Removing an Executor

To remove an executor from the central miner, follow these steps:

1. Run the following command to remove the executor:

    ```bash
    docker exec <docker instance> python /root/app/src/cli.py remove-executor --address <executor public ip> --port <executor external port>
    ```


### Monitoring earnings

To monitor your earnings, use [Taomarketcap.com](https://taomarketcap.com/subnets/51/miners)'s subnet 51 miner page to track your daily rewards, and relative performance with other miners.
