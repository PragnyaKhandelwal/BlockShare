// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title NodeRegistry
/// @notice On-chain directory of BlockShare storage nodes. Clients that cannot
///         reach the Django coordinator (e.g. the CLI in "Django down" mode)
///         discover the data plane from here.
contract NodeRegistry {
    struct StorageNode {
        string nodeId;     // e.g. "node-a"
        string endpoint;   // host:port of the node's BSP (TCP) data service
        bool active;
        uint64 registeredAt;
    }

    address public admin;
    StorageNode[] private nodes;
    mapping(bytes32 => uint256) private indexPlusOne; // keccak256(nodeId) => index + 1

    event NodeRegistered(uint256 indexed index, string nodeId, string endpoint);
    event NodeUpdated(uint256 indexed index, string nodeId, string endpoint, bool active);

    modifier onlyAdmin() {
        require(msg.sender == admin, "NodeRegistry: caller is not admin");
        _;
    }

    constructor() {
        admin = msg.sender;
    }

    /// @notice Register a node, or update its endpoint and re-activate it if it already exists.
    function registerNode(string calldata nodeId, string calldata endpoint) external onlyAdmin returns (uint256) {
        require(bytes(nodeId).length > 0, "NodeRegistry: empty node id");
        bytes32 key = keccak256(bytes(nodeId));
        uint256 existing = indexPlusOne[key];
        if (existing != 0) {
            StorageNode storage n = nodes[existing - 1];
            n.endpoint = endpoint;
            n.active = true;
            emit NodeUpdated(existing - 1, nodeId, endpoint, true);
            return existing - 1;
        }
        nodes.push(StorageNode(nodeId, endpoint, true, uint64(block.timestamp)));
        indexPlusOne[key] = nodes.length;
        emit NodeRegistered(nodes.length - 1, nodeId, endpoint);
        return nodes.length - 1;
    }

    function setNodeActive(string calldata nodeId, bool active) external onlyAdmin {
        uint256 i = indexPlusOne[keccak256(bytes(nodeId))];
        require(i != 0, "NodeRegistry: unknown node");
        StorageNode storage n = nodes[i - 1];
        n.active = active;
        emit NodeUpdated(i - 1, n.nodeId, n.endpoint, active);
    }

    function nodeCount() external view returns (uint256) {
        return nodes.length;
    }

    function getNode(uint256 index) external view returns (StorageNode memory) {
        return nodes[index];
    }

    function getNodes() external view returns (StorageNode[] memory) {
        return nodes;
    }
}
