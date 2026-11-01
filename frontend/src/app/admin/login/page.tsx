import { CenteredCard } from "@/components/centered-card";
import { LoginForm } from "./login-form";

export const metadata = { title: "Platform Admin · BizFlow AI" };

export default function AdminLoginPage() {
  return (
    <CenteredCard title="Platform Admin" subtitle="Sign in to manage BizFlow AI's brands">
      <LoginForm />
    </CenteredCard>
  );
}
