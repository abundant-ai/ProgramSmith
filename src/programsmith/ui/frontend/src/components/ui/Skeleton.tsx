import MuiSkeleton from "@mui/material/Skeleton";

export function Skeleton({ className }: { className?: string }) {
  return <MuiSkeleton animation="wave" className={className} variant="rectangular" />;
}
