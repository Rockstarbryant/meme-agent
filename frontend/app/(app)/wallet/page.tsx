import { WalletPanel } from "@/components/features/wallet-panel";
import { PageHeader } from "@/components/page-header";

export default function Page() {
  return (
    <>
      <PageHeader title="Wallet" description="Balance, deposit, withdraw and transaction history. Spending policy and signing are under Settings." />
      <WalletPanel />
    </>
  );
}
