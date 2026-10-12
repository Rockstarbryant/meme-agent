"""Current official Uniswap Arc deployment constants.

These are verified against Uniswap's current deployment and v4 deployment pages.
They are execution allowlist entries, not merely historical evidence.
"""
ARC_UNIVERSAL_ROUTER = "0x4fcA4a51Ab4F23A7447b3284fBd7D73289A89Fb1"
ARC_POOL_MANAGER = "0x8366a39CC670B4001A1121B8F6A443A643e40951"
ARC_QUOTER = "0x8Dc178eFB8111BB0973Dd9d722ebeFF267c98F94"
ARC_POSITION_MANAGER = "0x6049c9a0e26405C0985f9E3685C87d0aE917f82B"
ARC_PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
# Trading API proxy (CREATE2, same address on every chain). Used when the client
# sends x-permit2-disabled so wallets without EIP-712 can approve→swap.
ARC_UNISWAP_PROXY = "0x0000000085E102724e78eCd2F45DC9cA239Affad"

UNISWAP_ARC_CANDIDATES = {
    "universal_router": ARC_UNIVERSAL_ROUTER,
    "pool_manager": ARC_POOL_MANAGER,
    "quoter": ARC_QUOTER,
    "position_manager": ARC_POSITION_MANAGER,
    "permit2": ARC_PERMIT2,
    "uniswap_proxy": ARC_UNISWAP_PROXY,
}
STATUS = "verified_official_deployments"
SOURCE = "https://developers.uniswap.org/deployments and https://developers.uniswap.org/docs/protocols/v4/deployments"

def describe() -> dict:
    return {"status": STATUS, "source": SOURCE, "addresses": UNISWAP_ARC_CANDIDATES,
            "router_allowlist": [ARC_UNIVERSAL_ROUTER, ARC_UNISWAP_PROXY]}
