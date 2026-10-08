import { Positions } from "@/components/features/positions";
import { PageHeader } from "@/components/page-header";

export default function Page() {
  return (
    <>
      <PageHeader title="Positions" description="Open and closed trades with when they opened, why they closed, and the result. Paper and Live are kept apart." />
      <Positions />
    </>
  );
}
