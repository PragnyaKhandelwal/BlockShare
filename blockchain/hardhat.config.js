require("@nomicfoundation/hardhat-chai-matchers");
require("@nomicfoundation/hardhat-ethers");

const networks = {
  hardhat: { chainId: 31337 },
  localhost: { url: process.env.BLOCKCHAIN_RPC_URL || "http://127.0.0.1:8545" },
};

// Optional public testnet deployment: set SEPOLIA_RPC_URL and DEPLOYER_PRIVATE_KEY
if (process.env.SEPOLIA_RPC_URL && process.env.DEPLOYER_PRIVATE_KEY) {
  networks.sepolia = { url: process.env.SEPOLIA_RPC_URL, accounts: [process.env.DEPLOYER_PRIVATE_KEY] };
}

/** @type import('hardhat/config').HardhatUserConfig */
module.exports = {
  solidity: {
    version: "0.8.24",
    settings: { optimizer: { enabled: true, runs: 200 }, evmVersion: "cancun" },
  },
  networks,
};
