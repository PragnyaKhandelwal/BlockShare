const { expect } = require("chai");
const { ethers } = require("hardhat");

const READ = 1;
const WRITE = 2;
const OWNER = 3;

const sha256 = (data) => ethers.sha256(data);
const id = (s) => ethers.id(s); // keccak256 of a string -> bytes32 file id

// Same Merkle construction as coordination/merkle.py: SHA-256 leaves,
// sorted-pair SHA-256 parents, odd node promoted to the next level.
function merkle(leaves) {
  let level = leaves.slice();
  const levels = [level];
  while (level.length > 1) {
    const next = [];
    for (let i = 0; i < level.length; i += 2) {
      if (i + 1 === level.length) {
        next.push(level[i]);
        continue;
      }
      const [a, b] = [level[i], level[i + 1]].sort((x, y) => (BigInt(x) < BigInt(y) ? -1 : 1));
      next.push(sha256(ethers.concat([a, b])));
    }
    level = next;
    levels.push(level);
  }
  return { root: level[0], levels };
}

function proof(levels, index) {
  const out = [];
  for (let l = 0; l < levels.length - 1; l++) {
    const level = levels[l];
    const sibling = index ^ 1;
    if (sibling < level.length) out.push(level[sibling]);
    index = Math.floor(index / 2);
  }
  return out;
}

async function deployAll() {
  const [admin, alice, bob, charlie, mallory] = await ethers.getSigners();
  const nodes = await ethers.deployContract("NodeRegistry");
  const registry = await ethers.deployContract("FileRegistry");
  const sharing = await ethers.deployContract("FileSharing", [await registry.getAddress()]);
  await registry.setSharingContract(await sharing.getAddress());
  return { admin, alice, bob, charlie, mallory, nodes, registry, sharing };
}

const chunks = ["chunk-1", "chunk-2", "chunk-3", "chunk-4", "chunk-5"].map((c) => sha256(ethers.toUtf8Bytes(c)));
const tree = merkle(chunks);
const MANIFEST = sha256(ethers.toUtf8Bytes("manifest-v1"));
const F001 = id("F001");

describe("NodeRegistry", function () {
  it("registers storage nodes and toggles their status (admin only)", async function () {
    const { nodes, alice } = await deployAll();
    await expect(nodes.registerNode("node-a", "10.0.0.11:9000")).to.emit(nodes, "NodeRegistered");
    await nodes.registerNode("node-b", "10.0.0.12:9000");
    expect(await nodes.nodeCount()).to.equal(2);

    // re-registering updates the endpoint instead of duplicating
    await nodes.registerNode("node-a", "10.0.0.21:9000");
    expect(await nodes.nodeCount()).to.equal(2);
    expect((await nodes.getNode(0)).endpoint).to.equal("10.0.0.21:9000");

    await nodes.setNodeActive("node-b", false);
    const all = await nodes.getNodes();
    expect(all[1].active).to.equal(false);

    await expect(nodes.connect(alice).registerNode("evil", "1.2.3.4:1")).to.be.revertedWith(
      "NodeRegistry: caller is not admin"
    );
  });
});

describe("FileRegistry", function () {
  it("registers a file with its Merkle root and makes the sender the owner", async function () {
    const { registry, alice } = await deployAll();
    await expect(registry.connect(alice).registerFile(F001, "project.zip", tree.root, MANIFEST, 1000, 5))
      .to.emit(registry, "FileRegistered")
      .withArgs(F001, alice.address, "project.zip", tree.root);

    expect(await registry.ownerOf(F001)).to.equal(alice.address);
    const f = await registry.getFile(F001);
    expect(f.latestVersion).to.equal(1);
    const v = await registry.getVersion(F001, 1);
    expect(v.merkleRoot).to.equal(tree.root);
    expect(v.chunkCount).to.equal(5);
    expect(await registry.fileCount()).to.equal(1);
  });

  it("rejects duplicate file ids and empty roots", async function () {
    const { registry, alice, bob } = await deployAll();
    await registry.connect(alice).registerFile(F001, "a", tree.root, MANIFEST, 1, 1);
    await expect(registry.connect(bob).registerFile(F001, "b", tree.root, MANIFEST, 1, 1)).to.be.revertedWith(
      "FileRegistry: file id already registered"
    );
    await expect(registry.connect(bob).registerFile(id("x"), "b", ethers.ZeroHash, MANIFEST, 1, 1)).to.be.revertedWith(
      "FileRegistry: empty merkle root"
    );
  });

  it("keeps a version chain V1 -> V2 -> V3", async function () {
    const { registry, alice } = await deployAll();
    await registry.connect(alice).registerFile(F001, "project.zip", tree.root, MANIFEST, 1000, 5);
    const root2 = sha256(ethers.toUtf8Bytes("v2"));
    const root3 = sha256(ethers.toUtf8Bytes("v3"));
    await expect(registry.connect(alice).addVersion(F001, root2, MANIFEST, 1200, 6))
      .to.emit(registry, "VersionAdded")
      .withArgs(F001, 2, root2, MANIFEST, alice.address);
    await registry.connect(alice).addVersion(F001, root3, MANIFEST, 1300, 7);

    expect((await registry.getFile(F001)).latestVersion).to.equal(3);
    expect((await registry.getVersion(F001, 1)).merkleRoot).to.equal(tree.root);
    expect((await registry.getVersion(F001, 3)).merkleRoot).to.equal(root3);
    await expect(registry.getVersion(F001, 4)).to.be.revertedWith("FileRegistry: unknown version");
  });

  it("transfers ownership and updates filesOf()", async function () {
    const { registry, alice, charlie, mallory } = await deployAll();
    await registry.connect(alice).registerFile(F001, "project.zip", tree.root, MANIFEST, 1000, 5);
    await expect(registry.connect(mallory).transferOwnership(F001, mallory.address)).to.be.revertedWith(
      "FileRegistry: caller is not the file owner"
    );
    await expect(registry.connect(alice).transferOwnership(F001, charlie.address))
      .to.emit(registry, "OwnershipTransferred")
      .withArgs(F001, alice.address, charlie.address);
    expect(await registry.ownerOf(F001)).to.equal(charlie.address);
    expect(await registry.filesOf(alice.address)).to.deep.equal([]);
    expect(await registry.filesOf(charlie.address)).to.deep.equal([F001]);
  });

  it("verifies the whole-file root and individual chunks with Merkle proofs", async function () {
    const { registry, alice } = await deployAll();
    await registry.connect(alice).registerFile(F001, "project.zip", tree.root, MANIFEST, 1000, 5);

    expect(await registry.verifyRoot(F001, 1, tree.root)).to.equal(true);
    expect(await registry.verifyRoot(F001, 1, sha256(ethers.toUtf8Bytes("tampered")))).to.equal(false);

    for (let i = 0; i < chunks.length; i++) {
      expect(await registry.verifyChunk(F001, 1, chunks[i], proof(tree.levels, i))).to.equal(true);
    }
    const tampered = sha256(ethers.toUtf8Bytes("chunk-7-modified"));
    expect(await registry.verifyChunk(F001, 1, tampered, proof(tree.levels, 2))).to.equal(false);
  });

  it("soft-deletes files", async function () {
    const { registry, alice } = await deployAll();
    await registry.connect(alice).registerFile(F001, "project.zip", tree.root, MANIFEST, 1000, 5);
    await expect(registry.connect(alice).deleteFile(F001)).to.emit(registry, "FileDeleted");
    expect(await registry.exists(F001)).to.equal(false);
    expect(await registry.ownerOf(F001)).to.equal(ethers.ZeroAddress);
  });
});

describe("FileSharing", function () {
  it("lets the owner grant, update and revoke permissions", async function () {
    const { registry, sharing, alice, bob, charlie } = await deployAll();
    await registry.connect(alice).registerFile(F001, "project.zip", tree.root, MANIFEST, 1000, 5);

    await expect(sharing.connect(alice).shareFile(F001, bob.address, READ))
      .to.emit(sharing, "AccessGranted")
      .withArgs(F001, alice.address, bob.address, READ);
    expect(await sharing.checkPermission(F001, bob.address)).to.equal(READ);
    expect(await sharing.checkPermission(F001, alice.address)).to.equal(OWNER);
    expect(await sharing.canRead(F001, bob.address)).to.equal(true);
    expect(await sharing.canWrite(F001, bob.address)).to.equal(false);
    expect(await sharing.canRead(F001, charlie.address)).to.equal(false);

    await sharing.connect(alice).updatePermission(F001, bob.address, WRITE);
    expect(await sharing.canWrite(F001, bob.address)).to.equal(true);

    const [users, perms] = await sharing.getGrantees(F001);
    expect(users).to.deep.equal([bob.address]);
    expect(perms.map(Number)).to.deep.equal([WRITE]);
    expect(await sharing.getFilesSharedWith(bob.address)).to.deep.equal([F001]);

    await expect(sharing.connect(alice).revokeAccess(F001, bob.address)).to.emit(sharing, "AccessRevoked");
    expect(await sharing.canRead(F001, bob.address)).to.equal(false);
    expect(await sharing.getFilesSharedWith(bob.address)).to.deep.equal([]);
  });

  it("blocks non-owners from sharing and enforces WRITE for new versions", async function () {
    const { registry, sharing, alice, bob, mallory } = await deployAll();
    await registry.connect(alice).registerFile(F001, "project.zip", tree.root, MANIFEST, 1000, 5);

    await expect(sharing.connect(mallory).shareFile(F001, mallory.address, READ)).to.be.revertedWith(
      "FileSharing: caller is not the file owner"
    );
    await expect(sharing.connect(alice).shareFile(F001, bob.address, OWNER)).to.be.revertedWith(
      "FileSharing: invalid permission"
    );

    const root2 = sha256(ethers.toUtf8Bytes("v2"));
    await sharing.connect(alice).shareFile(F001, bob.address, READ);
    await expect(registry.connect(bob).addVersion(F001, root2, MANIFEST, 1, 1)).to.be.revertedWith(
      "FileRegistry: no write permission"
    );
    await sharing.connect(alice).shareFile(F001, bob.address, WRITE);
    await expect(registry.connect(bob).addVersion(F001, root2, MANIFEST, 1, 1))
      .to.emit(registry, "VersionAdded")
      .withArgs(F001, 2, root2, MANIFEST, bob.address);
  });

  it("follows ownership transfers: the new owner controls sharing", async function () {
    const { registry, sharing, alice, bob, charlie } = await deployAll();
    await registry.connect(alice).registerFile(F001, "project.zip", tree.root, MANIFEST, 1000, 5);
    await registry.connect(alice).transferOwnership(F001, charlie.address);

    await expect(sharing.connect(alice).shareFile(F001, bob.address, READ)).to.be.revertedWith(
      "FileSharing: caller is not the file owner"
    );
    await sharing.connect(charlie).shareFile(F001, bob.address, READ);
    expect(await sharing.checkPermission(F001, charlie.address)).to.equal(OWNER);
    expect(await sharing.checkPermission(F001, alice.address)).to.equal(0);
  });
});
