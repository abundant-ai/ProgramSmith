import type { ReactNode } from "react";
import MuiTooltip from "@mui/material/Tooltip";

export function Tooltip({
  content,
  children,
  side = "top",
  className,
}: {
  content: ReactNode;
  children: ReactNode;
  side?: "top" | "bottom";
  className?: string;
}) {
  if (!content) return <>{children}</>;
  return (
    <MuiTooltip title={content} placement={side} arrow>
      <span className={className ?? "inline-flex"}>{children}</span>
    </MuiTooltip>
  );
}
