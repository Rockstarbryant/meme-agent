/** Thin EIP-1193 helpers. The backend never sees keys: the user's wallet signs and sends. */
import type { ChainParams, SigningRequest } from "@/types/api";

interface Eip1193 { request(args: { method: string; params?: unknown[] }): Promise<unknown> }
declare global { interface Window { ethereum?: Eip1193 } }

export class WalletError extends Error {}

function provider(): Eip1193 {
  if (typeof window === "undefined" || !window.ethereum) throw new WalletError("No browser wallet found. Install MetaMask or another EIP-1193 wallet.");
  return window.ethereum;
}

export async function requestAccount(): Promise<string> {
  const accts = (await provider().request({ method: "eth_requestAccounts" })) as string[];
  if (!accts?.length) throw new WalletError("The wallet did not return an account.");
  return accts[0];
}

/** Switch to Arc, adding it (with the documented network parameters) if the wallet doesn't know it yet. */
export async function ensureArcNetwork(params: ChainParams): Promise<void> {
  const p = provider();
  try {
    await p.request({ method: "wallet_switchEthereumChain", params: [{ chainId: params.chainId }] });
  } catch (e) {
    if ((e as { code?: number }).code === 4902) await p.request({ method: "wallet_addEthereumChain", params: [params] });
    else throw e;
  }
}

const toHex = (s: string) => "0x" + Array.from(new TextEncoder().encode(s)).map((b) => b.toString(16).padStart(2, "0")).join("");

export async function personalSign(address: string, message: string): Promise<string> {
  return (await provider().request({ method: "personal_sign", params: [toHex(message), address] })) as string;
}

export async function sendSigningRequest(from: string, r: SigningRequest): Promise<string> {
  return (await provider().request({
    method: "eth_sendTransaction",
    params: [{ from, to: r.tx.to, data: r.tx.data, value: "0x" + BigInt(r.tx.value).toString(16), chainId: "0x" + r.tx.chain_id.toString(16) }],
  })) as string;
}
