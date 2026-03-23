{{- define "ack-s3-controller.bucket_policy" -}}
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":"*","Action":"s3:GetObject","Resource":"arn:aws:s3:::{{ .Values.oidc.bucketName }}/*"}]}
{{- end -}}
