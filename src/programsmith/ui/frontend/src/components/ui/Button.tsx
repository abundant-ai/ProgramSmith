import { forwardRef } from "react";
import MuiButton, { type ButtonProps as MuiButtonProps } from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";

type Variant = "primary" | "secondary" | "ghost" | "danger" | "outline";
type Size = "sm" | "md" | "lg";

interface ButtonProps extends Omit<MuiButtonProps, "color" | "size" | "variant"> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  target?: string;
  rel?: string;
}

const variantProps: Record<
  Variant,
  Pick<MuiButtonProps, "color" | "variant">
> = {
  primary: { color: "primary", variant: "contained" },
  secondary: { color: "inherit", variant: "outlined" },
  outline: { color: "inherit", variant: "outlined" },
  ghost: { color: "inherit", variant: "text" },
  danger: { color: "error", variant: "outlined" },
};

const sizes: Record<Size, MuiButtonProps["size"]> = {
  sm: "small",
  md: "medium",
  lg: "large",
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  (
    { variant = "secondary", size = "md", loading, children, disabled, ...rest },
    ref,
  ) => (
    <MuiButton
      ref={ref}
      size={sizes[size]}
      disabled={disabled || loading}
      startIcon={loading ? <CircularProgress color="inherit" size={14} /> : undefined}
      {...variantProps[variant]}
      {...rest}
    >
      {children}
    </MuiButton>
  ),
);
Button.displayName = "Button";
