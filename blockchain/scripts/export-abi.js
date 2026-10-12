// Copies ABI + bytecode of the compiled contracts into the Django
// `coordination/contracts` package so the backend can deploy and call them
// without needing Node.js at runtime.
const fs = require("fs");
const path = require("path");

const CONTRACTS = ["NodeRegistry", "FileRegistry", "FileSharing"];
const outDir = path.join(__dirname, "..", "..", "coordination", "contracts");
fs.mkdirSync(outDir, { recursive: true });

for (const name of CONTRACTS) {
  const artifact = require(path.join(__dirname, "..", "artifacts", "contracts", `${name}.sol`, `${name}.json`));
  const out = { contractName: name, abi: artifact.abi, bytecode: artifact.bytecode };
  fs.writeFileSync(path.join(outDir, `${name}.json`), JSON.stringify(out, null, 2));
  console.log(`exported ${name} -> coordination/contracts/${name}.json`);
}
