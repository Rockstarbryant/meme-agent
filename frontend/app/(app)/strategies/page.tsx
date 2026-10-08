import { Strategies } from "@/components/features/strategies";
import { PageHeader } from "@/components/page-header";

export default function Page() {
  return (
    <>
      <PageHeader title="Strategies" description="Choose which strategy the agent runs. Tuning its weights, thresholds and exits is under Settings." />
      <Strategies />
    </>
  );
}
