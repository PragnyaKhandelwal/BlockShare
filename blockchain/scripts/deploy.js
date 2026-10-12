// Deploys the BlockShare contract suite and writes the addresses to
// deployments/<network>.json. The Django backend can also deploy the same
// contracts itself (`python manage.py bootstrap_chain`); this script is for
// standalone use and public testnets.
const fs = require("fs");
const path = require("path");
const hre = require("hardhat");

async function main() {
  const [deployer] = await hre.ethers.getSigners();
  console.log(`Deploying with ${deployer.address} on ${hre.network.name}`);

  const nodeRegistry = await hre.ethers.deployContract("NodeRegistry");
  await nodeRegistry.waitForDeployment();

  const fileRegistry = await hre.ethers.deployContract("FileRegistry");
  await fileRegistry.waitForDeployment();

  const fileSharing = await hre.ethers.deployContract("FileSharing", [await fileRegistry.getAddress()]);
  await fileSharing.waitForDeployment();

  await (await fileRegistry.setSharingContract(await fileSharing.getAddress())).wait();

  const out = {
    network: hre.network.name,
    chainId: Number((await hre.ethers.provider.getNetwork()).chainId),
    deployer: deployer.address,
    NodeRegistry: await nodeRegistry.getAddress(),
    FileRegistry: await fileRegistry.getAddress(),
    FileSharing: await fileSharing.getAddress(),
    deployedAt: new Date().toISOString(),
  };
  const dir = path.join(__dirname, "..", "deployments");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, `${hre.network.name}.json`), JSON.stringify(out, null, 2));
  console.log(out);
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
