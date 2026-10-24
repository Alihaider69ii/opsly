import { Suspense } from "react";
import Link from "next/link";
import { CenteredCard } from "@/components/centered-card";
import { RedirectIfSignedIn } from "@/components/redirect-if-signed-in";
import { SignupForm } from "./signup-form";

export const metadata = { title: "Sign up · BizFlow AI" };

export default function SignupPage() {
  return (
    <CenteredCard
      title="Create your account"
      subtitle="Start automating your customer conversations"
    >
      <SignupForm />
      <p className="text-center text-sm text-zinc-500">
        Already have an account?{" "}
        <Link href="/login" className="font-medium text-zinc-900 underline dark:text-zinc-100">
          Log in
        </Link>
      </p>
      <Suspense>
        <RedirectIfSignedIn />
      </Suspense>
    </CenteredCard>
  );
}
