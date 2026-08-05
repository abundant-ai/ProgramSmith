import {
  forwardRef,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
} from "react";
import { cn } from "../../lib/cn";

export function Field({
  label,
  hint,
  htmlFor,
  children,
}: {
  label: string;
  hint?: ReactNode;
  htmlFor?: string;
  children: ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <label
        htmlFor={htmlFor}
        className="block text-[13px] font-medium text-ink-2"
      >
        {label}
      </label>
      {children}
      {hint && <p className="text-[12px] leading-snug text-ink-4">{hint}</p>}
    </div>
  );
}

export const Input = forwardRef<
  HTMLInputElement,
  InputHTMLAttributes<HTMLInputElement>
>(({ className, ...rest }, ref) => (
  <input
    ref={ref}
    className={cn(
      "h-10 w-full rounded-xl border border-line bg-bg-2/60 px-3.5 text-sm text-ink " +
        "placeholder:text-ink-4 transition-colors focus-ring focus:border-accent/50",
      className,
    )}
    {...rest}
  />
));
Input.displayName = "Input";

export const Select = forwardRef<
  HTMLSelectElement,
  SelectHTMLAttributes<HTMLSelectElement>
>(({ className, children, ...rest }, ref) => (
  <select
    ref={ref}
    className={cn(
      "h-10 w-full appearance-none rounded-xl border border-line bg-bg-2/60 px-3.5 text-sm text-ink " +
        "transition-colors focus-ring focus:border-accent/50 " +
        "bg-[length:16px] bg-[right_0.75rem_center] bg-no-repeat pr-9 " +
        "bg-[url('data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20width%3D%2224%22%20height%3D%2224%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22%23888%22%20stroke-width%3D%222%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22m6%209%206%206%206-6%22%2F%3E%3C%2Fsvg%3E')]",
      className,
    )}
    {...rest}
  >
    {children}
  </select>
));
Select.displayName = "Select";
