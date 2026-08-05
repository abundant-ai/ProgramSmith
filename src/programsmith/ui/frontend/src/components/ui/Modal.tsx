import type { CSSProperties, ReactNode } from "react";
import CloseIcon from "@mui/icons-material/Close";
import Dialog from "@mui/material/Dialog";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import IconButton from "@mui/material/IconButton";
import Typography from "@mui/material/Typography";
import { cn } from "../../lib/cn";

export function Modal({
  open,
  onClose,
  title,
  description,
  children,
  className,
  style,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
  center?: boolean;
}) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      fullWidth
      maxWidth="sm"
      slotProps={{ paper: { className: cn(className), style } }}
    >
      <DialogTitle className="flex items-start justify-between gap-4 border-b border-line px-6 py-5">
        <span className="min-w-0">
          <Typography component="span" variant="h2" className="block">
            {title}
          </Typography>
          {description && (
            <Typography component="span" variant="body2" color="text.secondary" className="mt-1 block">
              {description}
            </Typography>
          )}
        </span>
        <IconButton onClick={onClose} size="small" aria-label="Close">
          <CloseIcon fontSize="small" />
        </IconButton>
      </DialogTitle>
      <DialogContent className="px-6 py-5">{children}</DialogContent>
    </Dialog>
  );
}
