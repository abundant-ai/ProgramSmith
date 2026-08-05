import type { HTMLAttributes } from "react";
import Box from "@mui/material/Box";
import Paper from "@mui/material/Paper";
import { cn } from "../../lib/cn";

export function Card({
  className,
  hover,
  ...rest
}: HTMLAttributes<HTMLDivElement> & { hover?: boolean }) {
  return (
    <Paper
      component="div"
      square
      variant="outlined"
      className={cn(hover && "glass-hover", className)}
      {...rest}
    />
  );
}

export function CardHeader({
  className,
  ...rest
}: HTMLAttributes<HTMLDivElement>) {
  return (
    <Box
      component="div"
      className={cn(
        "flex items-center justify-between gap-3 border-b border-line px-5 py-4",
        className,
      )}
      {...rest}
    />
  );
}

export function CardTitle({
  className,
  ...rest
}: HTMLAttributes<HTMLHeadingElement>) {
  return (
    <Box
      component="h3"
      className={cn(
        "m-0 flex items-center gap-2 text-[12px] font-semibold uppercase tracking-[0.08em] text-ink-3",
        className,
      )}
      {...rest}
    />
  );
}

export function CardBody({
  className,
  ...rest
}: HTMLAttributes<HTMLDivElement>) {
  return <Box component="div" className={cn("p-5", className)} {...rest} />;
}
