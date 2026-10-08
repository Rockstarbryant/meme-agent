import { AgentControl } from "@/components/features/agent-control";
import { PageHeader } from "@/components/page-header";

export default function Page() {
  return (
    <>
      <PageHeader title="Agent" description="Start, pause or stop the agent and see what it is doing right now. Runner, strategy and limits are in Settings." />
      <AgentControl />
    </>
  );
}
