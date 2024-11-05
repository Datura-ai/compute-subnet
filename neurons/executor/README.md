# Executor

## Setup project
### Requirements
* Setup ubuntu on hardware with a 24GB minimum VRAM GPU
* CPU: 4 cores
* RAM: 8GB
* Storage: 50GB minimum

### Step 1: install Python is Python3
` sudo apt install python-is-python3 `

### Step 2: Install Docker
Add Docker's official GPG key:
```
sudo apt-get update
sudo apt-get install ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
```

Add the repository to Apt sources:
```
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update

sudo apt-get install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

test Docker install
`sudo docker run hello-world`

add user to docker group
```
sudo groupadd docker
sudo usermod -aG docker $USER
```

Logout and login

### Step 3: Install NVIDIA driver and CUDA driver

Cleanup any previous install
```
sudo apt autoremove cuda* nvidia* --purge
sudo /usr/bin/nvidia-uninstall
sudo /usr/local/cuda-X.Y/bin/cuda-uninstall
```

Install (for Ubuntu 24.04)
```
curl -fSsL https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/3bf863cc.pub | sudo gpg --dearmor | sudo tee /usr/share/keyrings/nvidia-drivers.gpg > /dev/null 2>&1

echo 'deb [signed-by=/usr/share/keyrings/nvidia-drivers.gpg] https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/ /' | sudo tee /etc/apt/sources.list.d/nvidia-drivers.list

sudo apt update
```

CUDA toolkit
```
sudo apt install nvidia-driver-<latest_version>
sudo apt install cuda-drivers-<latest_version>
sudo apt install cuda
sudo apt install -y nvidia-docker2
sudo apt-get install -y nvidia-container-toolkit

sudo reboot
```

### Step 4: Clone project

```
git clone https://github.com/Datura-ai/compute-subnet.git
```

### Step 5: Install Required Tools

Run following command to install required tools: 
```shell
cd compute-subnet && chmod +x scripts/install_executor_on_ubuntu.sh && scripts/install_executor_on_ubuntu.sh
```

if you don't have sudo on your machine, run
```shell
sed -i 's/sudo //g' scripts/install_executor_on_ubuntu.sh
```
to remove sudo from the setup script commands

### Step 6: Install Bittensor
```
python3 -m venv bt_venv
source bt_venv/bin/activate
pip install bittensor
```

### Step 7: Install and Run

* Go to executor root
```shell
cd neurons/executor
```

* Add .env in the project
```shell
cp .env.template .env
```

Add the correct miner wallet hotkey for `MINER_HOTKEY_SS58_ADDRESS`.
You can change the ports for `INTERNAL_PORT`, `EXTERNAL_PORT`, `SSH_PORT` based on your need.

```
INTERNAL_PORT: any non use internal port of your executor docker container
EXTERNAL_PORT: any non use external expose port of your executor docker container
SSH_PORT: ssh access port of your executor docker container
MINER_HOTKEY_SS58_ADDRESS: your SS58 hotkey address
```

* Run project
```shell
docker compose up -d
```
### Add Executor to Miner
You have to register to the subnet before adding the executor to the miner

`btcli subnet register --netuid <your_preferred_netuid>  --wallet.name  <your_coldkey> --wallet.hotkey <your_hotkey>`

On your Executor machine, run:
```
docker exec -it miners-miner-runner-1 sh

docker compose up -d
```

validator hotkey list: https://taomarketcap.com/subnets/51/validators
`docker exec miner-miner-1 python /root/app/src/cli.py add-executor --address <your-executor-public-ip-address> --port <your-executor-port> --validator <validator-hotkey-selected-from-list>`

