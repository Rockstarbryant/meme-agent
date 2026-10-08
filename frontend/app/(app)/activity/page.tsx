import { Activity } from "@/components/features/activity";
import { PageHeader } from "@/components/page-header";

export default function Page() {
  return (
    <>
      <PageHeader title="Activity" description="What the agent did and why: events from the current trading mode, plus a record of the changes you made." />
      <Activity />
    </>
  );
}
