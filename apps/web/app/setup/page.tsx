import Link from "next/link";
import { isR2Configured, isSupabaseConfigured } from "@/lib/env";
import { getUser } from "@/lib/supabase/server";
import { LanguageToggle } from "@/components/language-toggle";
import { PHButton, PHLogo } from "@/components/design-system";
import { PHSetupForm } from "@/components/setup/ph-setup-form";

// `isR2Configured()` reads server env, which must not be baked into a
// static prerender (a deploy with R2 set would otherwise serve a stale
// r2Configured=false and never light the upload path).
export const dynamic = "force-dynamic";

/**
 * Setup is the ProvenHire-branded JD-intake screen (see
 * components/setup/ph-setup-form.tsx for the full field list + submission
 * logic). Server component so it can resolve the signed-in user for the
 * sign-out affordance and R2 status without a client round-trip.
 */
export default async function SetupPage() {
  const r2Configured = isR2Configured();

  // Soft auth signal: surfaces a sign-out affordance when signed in. Page-level
  // gating lives on /interview-room; setup itself stays reachable in dev mode.
  const user = isSupabaseConfigured() ? await getUser() : null;

  return (
    <main className="ph-shell mx-auto min-h-screen max-w-[720px] px-6 py-12">
      <header className="flex items-center justify-between">
        <Link href="/" className="no-underline">
          <PHLogo compact />
        </Link>
        <div className="flex items-center gap-3">
          <LanguageToggle />
          {user && (
            <form action="/auth/signout" method="post">
              <PHButton type="submit" variant="ghost">
                Sign out
              </PHButton>
            </form>
          )}
        </div>
      </header>

      <PHSetupForm r2Configured={r2Configured} />
    </main>
  );
}
