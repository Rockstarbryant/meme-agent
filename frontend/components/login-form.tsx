"use client";
import { useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { toApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";

const MIN_PASSWORD = 10;

export function LoginForm() {
  const { login, register } = useAuth();
  const router = useRouter();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (mode === "register" && password.length < MIN_PASSWORD) { setError(`Password must be at least ${MIN_PASSWORD} characters.`); return; }
    setBusy(true);
    try {
      await (mode === "login" ? login : register)(email.trim(), password);
      router.replace("/");
    } catch (err) {
      setError(toApiError(err).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="w-full max-w-sm">
      <CardHeader><CardTitle className="text-base">{mode === "login" ? "Sign in" : "Create account"}</CardTitle>
        <p className="text-xs text-muted-foreground">New accounts start in PAPER mode. LIVE trading is off by default.</p></CardHeader>
      <CardContent>
        <form onSubmit={(e) => void submit(e)} className="space-y-3" noValidate>
          <div className="space-y-1"><Label htmlFor="email">Email</Label>
            <Input id="email" type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} required /></div>
          <div className="space-y-1"><Label htmlFor="password">Password</Label>
            <Input id="password" type="password" autoComplete={mode === "login" ? "current-password" : "new-password"} value={password} onChange={(e) => setPassword(e.target.value)} required /></div>
          {error && <Alert variant="destructive">{error}</Alert>}
          <Button type="submit" className="w-full" disabled={busy || !email || !password}>{mode === "login" ? "Sign in" : "Create account"}</Button>
          <button type="button" className="w-full text-center text-xs text-muted-foreground underline" onClick={() => { setError(null); setMode(mode === "login" ? "register" : "login"); }}>
            {mode === "login" ? "Need an account? Register" : "Have an account? Sign in"}
          </button>
        </form>
      </CardContent>
    </Card>
  );
}
