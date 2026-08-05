import type { ReactNode } from "react";
import { AlertTriangle } from "lucide-react";
import { Card } from "./Card";

export function EmptyState({
  icon,
  title,
  body,
  action,
}: {
  icon?: ReactNode;
  title: string;
  body?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <Card className="flex flex-col items-center justify-center px-6 py-16 text-center">
      {icon && (
        <div className="mb-4 flex size-14 items-center justify-center rounded-2xl bg-surface-2 text-ink-3">
          {icon}
        </div>
      )}
      <h3 className="text-lg font-semibold text-ink">{title}</h3>
      {body && <p className="mt-2 max-w-sm text-sm text-ink-3">{body}</p>}
      {action && <div className="mt-6">{action}</div>}
    </Card>
  );
}

export function ErrorState({
  title = "Something went wrong",
  message,
  action,
}: {
  title?: string;
  message?: string;
  action?: ReactNode;
}) {
  return (
    <Card className="flex flex-col items-center justify-center px-6 py-14 text-center">
      <div className="mb-4 flex size-12 items-center justify-center rounded-2xl bg-danger-soft/40 text-danger">
        <AlertTriangle className="size-6" />
      </div>
      <h3 className="text-base font-semibold text-ink">{title}</h3>
      {message && (
        <p className="mt-2 max-w-md text-sm text-ink-3">{message}</p>
      )}
      {action && <div className="mt-5">{action}</div>}
    </Card>
  );
}
