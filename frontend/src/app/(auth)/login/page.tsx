import { Suspense } from "react";
import Link from "next/link";
import { CenteredCard } from "@/components/centered-card";
import { RedirectIfSignedIn } from "@/components/redirect-if-signed-in";
import { LoginForm } from "./login-form";

export const metadata = { title: "Log in · BizFlow AI" };

export default function LoginPage() {
  return (
    <CenteredCard title="Welcome back" subtitle="Log in to your BizFlow AI account">
      <LoginForm />
      <p className="text-center text-sm text-zinc-500">
        New to BizFlow?{" "}
        <Link href="/signup" className="font-medium text-zinc-900 underline dark:text-zinc-100">
          Create an account
        </Link>
      </p>
      <Suspense>
        <RedirectIfSignedIn />
      </Suspense>
    </CenteredCard>
  );
}
