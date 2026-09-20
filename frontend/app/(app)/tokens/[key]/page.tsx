"use client";
import { useParams } from "next/navigation";
import { TokenDetail } from "@/components/features/token-detail";

export default function Page() {
  const { key } = useParams<{ key: string }>();
  return <TokenDetail tokenKey={decodeURIComponent(key)} />;
}
