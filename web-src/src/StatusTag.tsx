import { STATUS_LABEL, Status } from "./api";

export default function StatusTag({ status }: { status: Status }) {
  return <span className={`tag s-${status}`}>{STATUS_LABEL[status] ?? status}</span>;
}
