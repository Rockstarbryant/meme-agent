"use client";
import { useParams } from "next/navigation";
import { DecisionDetail } from "@/components/features/decision-detail";

export default function Page() {
  const { id } = useParams<{ id: string }>();
  return <DecisionDetail decisionId={id} />;
}
