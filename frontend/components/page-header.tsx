import type { ReactNode } from "react";

/** One consistent page title: serif heading, a sentence saying what the page is for, optional actions, and a gold-tipped rule. */
export function PageHeader({ title, description, actions }: { title: string; description?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="relative animate-fade-in border-b pb-6 md:pb-8">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <h1 className="font-serif text-[2rem] font-normal leading-[1.15] tracking-[-0.01em] md:text-[2.5rem]">{title}</h1>
          {description && <p className="mt-3 max-w-2xl text-base leading-[1.75] text-muted-foreground">{description}</p>}
        </div>
        {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
      </div>
      <span aria-hidden className="absolute -bottom-px left-0 h-0.5 w-16 bg-accent" />
    </header>
  );
}
