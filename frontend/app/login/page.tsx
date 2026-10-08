import { LoginForm } from "@/components/login-form";

export default function LoginPage() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-10 px-4 py-12">
      <div className="text-center">
        <p className="font-serif text-[2.5rem] leading-none tracking-[-0.02em]">Arc Agent</p>
        <span aria-hidden className="mx-auto mt-5 block h-px w-16 bg-accent" />
        <p className="mt-4 font-mono text-xs tracking-[0.15em] text-muted-foreground">autonomous trading</p>
      </div>
      <LoginForm />
    </main>
  );
}
