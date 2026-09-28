"""Public nodes for each network: where a new node finds the network, and where the wallet goes
when this computer runs no node.

Not part of consensus, and not trusted: a node checks everything any peer sends it, and the
wallet signs on this computer, so a node can't move your coins. A public node does learn which
addresses a wallet asks about. Mainnet's entries are added at launch.
"""

# `tc node` connects to these when you give no --peer (and then remembers what it learns in peers.json).
SEEDS: dict[str, tuple[str, ...]] = {
    "testnet": ("ws://testnet.talcash.com:64185/v1/p2p",),
}

# `tc wallet` uses this node's API when you give no --node and none answers on this computer.
# HTTPS (a reverse proxy in front of the node), so nobody on the way can read or change the answers.
PUBLIC_API: dict[str, str] = {
    "testnet": "https://testnet.talcash.com",
}
