import Link from "next/link";
import { Eyebrow } from "@/components/ui/eyebrow";
import { IntegrityForm } from "@/components/settings/integrity-form";

// The form fetches current settings client-side on mount; nothing here reads
// server env, but keep the page dynamic so it's never mistakenly prerendered
// with stale settings baked in.
export const dynamic = "force-dynamic";

/**
 * /settings/integrity — global Integrity Controls (proctoring) config.
 *
 * Open route, no auth gate: this OSS build has no roles/admin concept yet
 * (see IntegrityForm's header comment). A distribution that adds one can
 * gate this page the same way `middleware.ts` already gates others.
 */
export default function IntegritySettingsPage() {
  return (
    <main className="mx-auto max-w-[720px] px-6 py-12">
      <header className="mb-8 flex items-center justify-between">
        <Link href="/" className="no-underline">
          <Eyebrow>Proven Hire Job Description Interview</Eyebrow>
        </Link>
      </header>

      <IntegrityForm />
    </main>
  );
}
