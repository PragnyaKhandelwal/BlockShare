// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IFileSharing {
    function canWrite(bytes32 fileId, address user) external view returns (bool);
}

/// @title FileRegistry
/// @notice Decentralized source of truth for file ownership, version history
///         and content identity (Merkle roots) when BlockShare runs in
///         Blockchain mode. File bytes never touch the chain: each version
///         stores the Merkle root of its chunk hashes and the hash of its
///         chunk manifest, which itself lives on the storage nodes.
contract FileRegistry {
    struct FileRecord {
        address owner;
        string name;
        uint32 latestVersion;
        uint64 createdAt;
        bool deleted;
    }

    struct Version {
        bytes32 merkleRoot;    // root of the SHA-256 Merkle tree over chunk hashes
        bytes32 manifestHash;  // SHA-256 of the manifest object stored on the nodes
        uint64 size;           // bytes
        uint32 chunkCount;
        uint64 timestamp;
        address author;
    }

    address public admin;
    IFileSharing public sharing;
    uint256 public fileCount;

    mapping(bytes32 => FileRecord) private files;
    mapping(bytes32 => mapping(uint32 => Version)) private versions;
    mapping(address => bytes32[]) private ownerIndex; // append-only; filtered on read

    event FileRegistered(bytes32 indexed fileId, address indexed owner, string name, bytes32 merkleRoot);
    event VersionAdded(bytes32 indexed fileId, uint32 version, bytes32 merkleRoot, bytes32 manifestHash, address indexed author);
    event OwnershipTransferred(bytes32 indexed fileId, address indexed previousOwner, address indexed newOwner);
    event FileDeleted(bytes32 indexed fileId, address indexed by);

    modifier onlyAdmin() {
        require(msg.sender == admin, "FileRegistry: caller is not admin");
        _;
    }

    modifier onlyFileOwner(bytes32 fileId) {
        require(exists(fileId), "FileRegistry: unknown file");
        require(files[fileId].owner == msg.sender, "FileRegistry: caller is not the file owner");
        _;
    }

    constructor() {
        admin = msg.sender;
    }

    function setSharingContract(address sharingContract) external onlyAdmin {
        sharing = IFileSharing(sharingContract);
    }

    // ------------------------------------------------------------------ writes

    function registerFile(
        bytes32 fileId,
        string calldata name,
        bytes32 merkleRoot,
        bytes32 manifestHash,
        uint64 size,
        uint32 chunkCount
    ) external {
        require(fileId != bytes32(0), "FileRegistry: empty file id");
        require(files[fileId].owner == address(0), "FileRegistry: file id already registered");
        require(merkleRoot != bytes32(0), "FileRegistry: empty merkle root");

        files[fileId] = FileRecord(msg.sender, name, 1, uint64(block.timestamp), false);
        versions[fileId][1] = Version(merkleRoot, manifestHash, size, chunkCount, uint64(block.timestamp), msg.sender);
        ownerIndex[msg.sender].push(fileId);
        fileCount += 1;

        emit FileRegistered(fileId, msg.sender, name, merkleRoot);
        emit VersionAdded(fileId, 1, merkleRoot, manifestHash, msg.sender);
    }

    /// @notice Add a new version. Allowed for the owner and for users holding WRITE permission.
    function addVersion(
        bytes32 fileId,
        bytes32 merkleRoot,
        bytes32 manifestHash,
        uint64 size,
        uint32 chunkCount
    ) external returns (uint32) {
        require(exists(fileId), "FileRegistry: unknown file");
        require(merkleRoot != bytes32(0), "FileRegistry: empty merkle root");
        FileRecord storage f = files[fileId];
        require(
            f.owner == msg.sender || (address(sharing) != address(0) && sharing.canWrite(fileId, msg.sender)),
            "FileRegistry: no write permission"
        );
        uint32 v = f.latestVersion + 1;
        f.latestVersion = v;
        versions[fileId][v] = Version(merkleRoot, manifestHash, size, chunkCount, uint64(block.timestamp), msg.sender);
        emit VersionAdded(fileId, v, merkleRoot, manifestHash, msg.sender);
        return v;
    }

    function transferOwnership(bytes32 fileId, address newOwner) external onlyFileOwner(fileId) {
        require(newOwner != address(0), "FileRegistry: new owner is the zero address");
        address previous = files[fileId].owner;
        require(newOwner != previous, "FileRegistry: already the owner");
        files[fileId].owner = newOwner;
        ownerIndex[newOwner].push(fileId);
        emit OwnershipTransferred(fileId, previous, newOwner);
    }

    function deleteFile(bytes32 fileId) external onlyFileOwner(fileId) {
        files[fileId].deleted = true;
        emit FileDeleted(fileId, msg.sender);
    }

    // ------------------------------------------------------------------- reads

    function exists(bytes32 fileId) public view returns (bool) {
        return files[fileId].owner != address(0) && !files[fileId].deleted;
    }

    function ownerOf(bytes32 fileId) external view returns (address) {
        return exists(fileId) ? files[fileId].owner : address(0);
    }

    function getFile(bytes32 fileId) external view returns (FileRecord memory) {
        require(files[fileId].owner != address(0), "FileRegistry: unknown file");
        return files[fileId];
    }

    function getVersion(bytes32 fileId, uint32 version) external view returns (Version memory) {
        require(version >= 1 && version <= files[fileId].latestVersion, "FileRegistry: unknown version");
        return versions[fileId][version];
    }

    /// @notice Files currently owned (and not deleted) by `owner`.
    function filesOf(address owner) external view returns (bytes32[] memory) {
        bytes32[] storage idx = ownerIndex[owner];
        uint256 count;
        for (uint256 i = 0; i < idx.length; i++) {
            if (_ownedNow(idx[i], owner, idx, i)) count++;
        }
        bytes32[] memory out = new bytes32[](count);
        uint256 j;
        for (uint256 i = 0; i < idx.length; i++) {
            if (_ownedNow(idx[i], owner, idx, i)) out[j++] = idx[i];
        }
        return out;
    }

    /// @notice Compare a recomputed Merkle root with the registered one.
    function verifyRoot(bytes32 fileId, uint32 version, bytes32 merkleRoot) external view returns (bool) {
        return merkleRoot != bytes32(0) && versions[fileId][version].merkleRoot == merkleRoot;
    }

    /// @notice Verify that a single chunk (leaf = SHA-256 of the chunk) belongs to a version,
    ///         using a Merkle proof built with sorted-pair SHA-256 hashing.
    function verifyChunk(bytes32 fileId, uint32 version, bytes32 leaf, bytes32[] calldata proof) external view returns (bool) {
        bytes32 h = leaf;
        for (uint256 i = 0; i < proof.length; i++) {
            bytes32 p = proof[i];
            h = h < p ? sha256(abi.encodePacked(h, p)) : sha256(abi.encodePacked(p, h));
        }
        bytes32 root = versions[fileId][version].merkleRoot;
        return root != bytes32(0) && h == root;
    }

    // a file id can appear several times in the index after transfers back and forth;
    // only count its first occurrence while it is still owned by `owner`
    function _ownedNow(bytes32 fileId, address owner, bytes32[] storage idx, uint256 pos) private view returns (bool) {
        if (!exists(fileId) || files[fileId].owner != owner) return false;
        for (uint256 k = 0; k < pos; k++) {
            if (idx[k] == fileId) return false;
        }
        return true;
    }
}
