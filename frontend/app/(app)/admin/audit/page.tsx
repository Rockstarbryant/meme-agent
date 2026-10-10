import { AdminAudit } from "@/components/features/admin-audit";
import { PageHeader } from "@/components/page-header";

export default function Page() {
  return (
    <>
      <PageHeader title="Audit log" description="Admin only. Which provider failed, where, when; which AI answered; which source produced each figure; why a live order was rejected." />
      <AdminAudit />
    </>
  );
}
