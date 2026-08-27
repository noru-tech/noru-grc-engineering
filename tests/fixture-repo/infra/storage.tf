# Fixture Terraform for the iac-scan collector. Nothing here is deployed and nothing here is real;
# it exists so the scan has something to find and the tests have something to assert on.

resource "aws_s3_bucket" "public_assets" {
  bucket = "example-public-assets"
  acl    = "public-read"
}

resource "aws_db_instance" "primary" {
  identifier     = "example-primary"
  engine         = "postgres"
  instance_class = "db.t3.micro"
  password       = "example-placeholder-not-a-credential"
}
