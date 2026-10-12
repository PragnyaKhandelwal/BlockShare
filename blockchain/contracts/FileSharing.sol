// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IFileRegistry {
    function ownerOf(bytes32 fileId) external view returns (address);
    function exists(bytes32 fileId) external view returns (bool);
}

/// @title FileSharing
/// @notice Smart-contract based permission management. In Blockchain mode the
///         BlockShare coordinator asks this contract "may X read file F?"
///         before it fetches any chunk from the storage nodes.
contract FileSharing {
    enum Permission { NONE, READ, WRITE, OWNER }

    IFileRegistry public immutable registry;

    mapping(bytes32 => mapping(address => Permission)) private grants;
    mapping(bytes32 => address[]) private granteeList;
    mapping(bytes32 => mapping(address => bool)) private granteeListed;
    mapping(address => bytes32[]) private sharedWith;
    mapping(address => mapping(bytes32 => bool)) private sharedListed;

    event AccessGranted(bytes32 indexed fileId, address indexed owner, address indexed grantee, Permission permission);
    event AccessRevoked(bytes32 indexed fileId, address indexed owner, address indexed grantee);

    modifier onlyFileOwner(bytes32 fileId) {
        require(registry.exists(fileId), "FileSharing: unknown file");
        require(registry.ownerOf(fileId) == msg.sender, "FileSharing: caller is not the file owner");
        _;
    }

    constructor(address fileRegistry) {
        registry = IFileRegistry(fileRegistry);
    }

    // ------------------------------------------------------------------ writes

    /// @notice Grant (or change) READ (1) / WRITE (2) access to `grantee`.
    function shareFile(bytes32 fileId, address grantee, Permission permission) public onlyFileOwner(fileId) {
        require(grantee != address(0), "FileSharing: grantee is the zero address");
        require(grantee != msg.sender, "FileSharing: owner already has full access");
        require(permission == Permission.READ || permission == Permission.WRITE, "FileSharing: invalid permission");

        grants[fileId][grantee] = permission;
        if (!granteeListed[fileId][grantee]) {
            granteeListed[fileId][grantee] = true;
            granteeList[fileId].push(grantee);
        }
        if (!sharedListed[grantee][fileId]) {
            sharedListed[grantee][fileId] = true;
            sharedWith[grantee].push(fileId);
        }
        emit AccessGranted(fileId, msg.sender, grantee, permission);
    }

    function updatePermission(bytes32 fileId, address grantee, Permission permission) external {
        require(grants[fileId][grantee] != Permission.NONE, "FileSharing: no existing grant");
        shareFile(fileId, grantee, permission);
    }

    function revokeAccess(bytes32 fileId, address grantee) external onlyFileOwner(fileId) {
        require(grants[fileId][grantee] != Permission.NONE, "FileSharing: no existing grant");
        grants[fileId][grantee] = Permission.NONE;
        emit AccessRevoked(fileId, msg.sender, grantee);
    }

    // ------------------------------------------------------------------- reads

    function checkPermission(bytes32 fileId, address user) public view returns (Permission) {
        if (!registry.exists(fileId)) return Permission.NONE;
        if (registry.ownerOf(fileId) == user) return Permission.OWNER;
        return grants[fileId][user];
    }

    function canRead(bytes32 fileId, address user) external view returns (bool) {
        return checkPermission(fileId, user) != Permission.NONE;
    }

    function canWrite(bytes32 fileId, address user) external view returns (bool) {
        Permission p = checkPermission(fileId, user);
        return p == Permission.WRITE || p == Permission.OWNER;
    }

    /// @notice Active grants of a file (excludes revoked users).
    function getGrantees(bytes32 fileId) external view returns (address[] memory users, Permission[] memory permissions) {
        address[] storage all = granteeList[fileId];
        uint256 count;
        for (uint256 i = 0; i < all.length; i++) {
            if (grants[fileId][all[i]] != Permission.NONE) count++;
        }
        users = new address[](count);
        permissions = new Permission[](count);
        uint256 j;
        for (uint256 i = 0; i < all.length; i++) {
            Permission p = grants[fileId][all[i]];
            if (p != Permission.NONE) {
                users[j] = all[i];
                permissions[j] = p;
                j++;
            }
        }
    }

    /// @notice Files `user` can currently access through a grant (not ownership).
    function getFilesSharedWith(address user) external view returns (bytes32[] memory) {
        bytes32[] storage all = sharedWith[user];
        uint256 count;
        for (uint256 i = 0; i < all.length; i++) {
            if (_activeGrant(all[i], user)) count++;
        }
        bytes32[] memory out = new bytes32[](count);
        uint256 j;
        for (uint256 i = 0; i < all.length; i++) {
            if (_activeGrant(all[i], user)) out[j++] = all[i];
        }
        return out;
    }

    function _activeGrant(bytes32 fileId, address user) private view returns (bool) {
        return registry.exists(fileId) && registry.ownerOf(fileId) != user && grants[fileId][user] != Permission.NONE;
    }
}
