# Fixture Terraform for the iac-scan collector. Nothing here is deployed.

resource "aws_security_group" "web" {
  name = "example-web"

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
