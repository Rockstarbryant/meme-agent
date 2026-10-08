import { Suspense } from "react";
import { SettingsView } from "@/components/features/settings";
import { PageHeader } from "@/components/page-header";
import { Loading } from "@/components/states";

export default function Page() {
  return (
    <>
      <PageHeader title="Settings" description="Everything you can configure, in one place: strategy, limits, wallet policy, runner, AI and notifications." />
      <Suspense fallback={<Loading />}><SettingsView /></Suspense>
    </>
  );
}
