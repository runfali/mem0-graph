"use client";

import { useEffect, useState } from "react";
import Image from "next/image";
import { useRouter, useSearchParams } from "next/navigation";

// 四轮审计：next 开放重定向防护——仅允许站内路径（/ 开头且不含协议/主机）
function safeNextTarget(raw: string | null): string {
  if (raw && raw.startsWith("/") && !raw.includes("://") && !raw.startsWith("//")) {
    return raw;
  }
  return "/dashboard";
}
import { useTheme } from "next-themes";
import { Check, Copy } from "lucide-react";
import { CopyToClipboard } from "react-copy-to-clipboard";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAuth } from "@/hooks/use-auth";
import { getErrorMessage } from "@/lib/error-message";
import { isValidEmail } from "@/lib/validators";

const RESET_COMMAND =
  "make reset-admin-password EMAIL=<your-email> PASSWORD=<new-password>";

export default function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { user, isLoading, login } = useAuth();
  const { resolvedTheme } = useTheme();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [mounted, setMounted] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (!isLoading && user) {
      router.push(safeNextTarget(searchParams.get("next")));
    }
  }, [user, isLoading, router, searchParams]);

  const emailValid = isValidEmail(email);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    if (!emailValid) {
      setError("请输入有效的邮箱地址。");
      return;
    }
    setSubmitting(true);
    try {
      await login(email, password);
      router.push(safeNextTarget(searchParams.get("next")));
    } catch (err) {
      setError(getErrorMessage(err, "登录失败"));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-screen">
      <div className="flex-1 bg-surface-default-primary flex items-center justify-center p-8">
        <div className="w-full max-w-md">
          <div className="flex justify-center mb-2">
            {mounted && (
              <Image
                src={
                  resolvedTheme === "dark"
                    ? "/images/logos/logo-light.png"
                    : "/images/logos/logo-dark.png"
                }
                alt="Mem0"
                width={41}
                height={41}
              />
            )}
          </div>
          <h1 className="text-2xl font-semibold text-onSurface-default-primary text-center mb-6 font-fustat">
            登录 Mem0
          </h1>
          <div className="flex flex-col gap-4 border p-8 border-memBorder-primary rounded-xl">
            {error && (
              <p className="text-sm text-onSurface-danger-primary bg-surface-danger-primary px-3 py-2 rounded">
                {error}
              </p>
            )}
            <form onSubmit={handleSubmit} className="flex flex-col gap-4">
              <div className="space-y-1.5">
                <Label htmlFor="login-email">邮箱</Label>
                <Input
                  id="login-email"
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="admin@company.com"
                  required
                  autoFocus
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="login-password">密码</Label>
                <Input
                  id="login-password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                />
              </div>
              <Button
                type="submit"
                disabled={submitting || !emailValid || !password}
                variant="default"
                size="lg"
                className="w-full"
              >
                {submitting ? "登录中..." : "登录"}
              </Button>
            </form>
            <Dialog>
              <DialogTrigger asChild>
                <button
                  type="button"
                  className="text-xs text-onSurface-default-tertiary hover:text-onSurface-default-primary underline underline-offset-4 self-center"
                >
                  忘记密码？
                </button>
              </DialogTrigger>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>重置管理员密码</DialogTitle>
                  <DialogDescription>
                    请在服务器主机上运行此命令。它会覆盖现有密码；已登录用户仍可
                    保持登录状态，直到会话过期。
                  </DialogDescription>
                </DialogHeader>
                <div className="flex gap-2">
                  <Input
                    readOnly
                    value={RESET_COMMAND}
                    className="font-mono text-xs"
                  />
                  <CopyToClipboard
                    text={RESET_COMMAND}
                    onCopy={() => {
                      setCopied(true);
                      setTimeout(() => setCopied(false), 2000);
                    }}
                  >
                    <Button variant="outline" size="icon">
                      {copied ? (
                        <Check className="size-4" />
                      ) : (
                        <Copy className="size-4" />
                      )}
                    </Button>
                  </CopyToClipboard>
                </div>
              </DialogContent>
            </Dialog>
          </div>
        </div>
      </div>

      <div className="relative hidden h-screen flex-1 items-center justify-center overflow-hidden bg-sentry-night px-10 lg:flex">
        <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_at_top_left,rgba(106,95,193,0.4),transparent_55%),radial-gradient(ellipse_at_bottom_right,rgba(250,127,170,0.18),transparent_50%)]" />
        <div className="pointer-events-none absolute inset-0 bg-[url('/images/dither.svg')] bg-bottom bg-no-repeat bg-contain" />
        <div className="pointer-events-none absolute top-0 left-0 h-[2px] w-full bg-gradient-to-r from-sentry-lime via-sentry-violet to-sentry-pink" />
        <div className="relative z-10 flex w-full max-w-[564px] flex-col items-center gap-20 text-center text-ink">
          <div className="w-full space-y-5">
            <p className="typo-h3 text-ink">
              &quot;Mem0
              帮助我们为每位学生实现了真正的个性化辅导，集成只花了一个周末。&quot;
            </p>
            <div className="flex flex-col items-center gap-[7px]">
              <div className="flex flex-col items-center gap-1">
                <p className="typo-body-sm text-ink-muted">Michael Tong</p>
                <p className="typo-body-xs text-ink-muted">CTO, RevisionDojo</p>
              </div>
              <Image
                src="/images/micheal.png"
                alt="Michael Tong"
                width={32}
                height={32}
                className="size-8 rounded-full object-cover"
              />
            </div>
          </div>
          <div className="flex w-full flex-col items-center gap-3">
            <p className="typo-body text-ink-muted">超过 10 万开发者信赖</p>
            <div className="flex items-center justify-center gap-8 text-ink">
              <div className="h-6 shrink-0">
                <Image
                  src="/images/logos/aws.svg"
                  alt="AWS"
                  width={41}
                  height={24}
                  className="size-full object-contain"
                />
              </div>
              <div className="h-5 shrink-0">
                <Image
                  src="/images/logos/nvidia.svg"
                  alt="NVIDIA"
                  width={109}
                  height={21}
                  className="size-full object-contain"
                />
              </div>
              <div className="h-[21px] shrink-0">
                <Image
                  src="/images/vercel.png"
                  alt="Vercel"
                  width={66}
                  height={21}
                  className="size-full object-contain"
                />
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
