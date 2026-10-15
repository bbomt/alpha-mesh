import { RemovalPolicy, Stack, StackProps } from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";

export interface CheckpointStackProps extends StackProps {
  deployEnv: string;
  checkpointTtlDays: number;
}

// Storage for LangGraph DynamoDBSaver checkpoints (PMAT-237 spec 3.10).
// No IAM resources: the agent runs on EC2 through the SSO dev-EC2 permission set.
// No S3 lifecycle rule: given ttl_seconds, the saver adds its own ttl-days rule.
export class CheckpointStack extends Stack {
  constructor(scope: Construct, id: string, props: CheckpointStackProps) {
    super(scope, id, props);

    const { deployEnv, checkpointTtlDays } = props;
    const isDev = deployEnv === "dev";
    const removalPolicy = isDev ? RemovalPolicy.DESTROY : RemovalPolicy.RETAIN;

    const table = new dynamodb.Table(this, "CheckpointTable", {
      tableName: `pmat_${deployEnv}_ALPHA_MESH_REVIEW_CHECKPOINTS`,
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      // DynamoDBSaver (langgraph-checkpoint-aws 1.2.3) writes "ttl" as epoch seconds (now + ttl_seconds)
      timeToLiveAttribute: "ttl",
      removalPolicy,
    });

    const bucket = new s3.Bucket(this, "CheckpointBucket", {
      bucketName: `pmat-${deployEnv}-alpha-mesh-review-checkpoints-${this.account}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      removalPolicy,
      autoDeleteObjects: isDev,
    });

    const prefix = `/pmat/${deployEnv}/alpha-mesh/review-pull-request`;
    new ssm.StringParameter(this, "CheckpointTableParam", {
      parameterName: `${prefix}/checkpoint-table`,
      stringValue: table.tableName,
    });
    new ssm.StringParameter(this, "CheckpointBucketParam", {
      parameterName: `${prefix}/checkpoint-bucket`,
      stringValue: bucket.bucketName,
    });
    new ssm.StringParameter(this, "CheckpointTtlDaysParam", {
      parameterName: `${prefix}/checkpoint-ttl-days`,
      stringValue: String(checkpointTtlDays),
    });
  }
}
