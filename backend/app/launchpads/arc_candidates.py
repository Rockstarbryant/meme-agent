from app.launchpads.registry import LaunchpadDescriptor

# Arc launchpad contracts verified by Bitquery's production Arc Launchpads API.
# The descriptor registry is metadata; discovery itself is implemented in BitqueryArcMarketData.
_ARGUS = "0xb021be536808f551b31789422fd28a6c9c6e97da"
_RADAR_CLASSIC = "0x4b638c1502a07a8e1a26112ee98f51a3f34bc93a"
_RADAR_REFLECTION = "0x2d933ce4bde6f3d99540b5d7886b383e59b2b2f8"
_TOLLY = "0xcad7ee36ac193bf2eddb7b3e2736c5bdb8269c8b"
_WARP = "0x0dcad158e98bc24455f9e94f46709d8a5f6d1255"
_ARCHEMIST = "0x297cebc4de347347205cd08667b56ee951dd8810"
_PEGD = "0xd0aa679ec263e8f9bc929426eb9eab2e061d2c5f"


def _d(name: str, address: str, event_hash: str, notes: str = "") -> LaunchpadDescriptor:
    return LaunchpadDescriptor(
        name=name, chain="arc", mainnet_live=True, verified=True, enabled=True,
        factory_address=address, token_creation_event=event_hash,
        quote_asset="USDC", api="Bitquery EVM.Events + Trading.*",
        ws_or_indexer="Bitquery streaming.bitquery.io/graphql",
        rpc_requirements="Arc EVM RPC",
        contract_verified=True, automation_feasible=True,
        notes=("Contract/event verified in Bitquery production Arc launchpad documentation. " + notes).strip(),
    )


def arc_candidates() -> list[LaunchpadDescriptor]:
    return [
        _d("argus", _ARGUS, "1d8917231579f8ce39407f0d616f36f357b07329b0ce5164d0754ac15145ce0a"),
        _d("radardex_classic", _RADAR_CLASSIC, "851d681a32f0efba577c4a1bd412f74b575764a6b91e499a05a48a23f3821d66"),
        _d("radardex_reflection", _RADAR_REFLECTION, "851d681a32f0efba577c4a1bd412f74b575764a6b91e499a05a48a23f3821d66"),
        _d("tolly", _TOLLY, "875522b092d9e19a1de359e4bd218090d582fa521c9733889acf1a5ff1941255"),
        _d("warp", _WARP, "0b4cfda446fdf9ec5a85855f088c154869eb62e3e723d7d80319b680f90e0cfd"),
        _d("archemist", _ARCHEMIST, "8e83c293b82cf6e864a90c1ccffea5e0f1ec23b271eff78e78f1dbd5e32a9c7d"),
        _d("pegd", _PEGD, "4b5a1abdb5ebec3e01fa29e6e1f5e3095f8b3cfaed9ffcd46ad35e7ebc58039e"),
    ]
