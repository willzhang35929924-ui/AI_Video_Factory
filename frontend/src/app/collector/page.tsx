"use client";
import { PageHeading } from "@/components/factory-ui";
import { IntegrationCollector } from "@/components/integration-collector";

export default function CollectorPage() {
  return <><PageHeading eyebrow="COLLECTION" title="采集控制台" description="选择数据来源，确认筛选规则，再开始生产。" /><IntegrationCollector /></>;
}
