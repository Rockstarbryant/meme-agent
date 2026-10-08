import { Opportunities } from "@/components/features/opportunities";
import { PageHeader } from "@/components/page-header";

export default function Page() {
  return (
    <>
      <PageHeader title="Opportunities" description="Tokens the agent evaluated in the last 24 hours for the current trading mode, and what it decided." />
      <Opportunities />
    </>
  );
}
