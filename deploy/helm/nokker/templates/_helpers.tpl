{{/* Names ------------------------------------------------------------------------------------------------- */}}
{{- define "nokker.fullname" -}}
{{- if contains "nokker" .Release.Name -}}
{{- .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-nokker" .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "nokker.labels" -}}
app.kubernetes.io/name: nokker
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/* Selector of one component: (dict "root" . "component" "core") */}}
{{- define "nokker.selector" -}}
app.kubernetes.io/name: nokker
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* Image: (dict "root" . "image" .Values.x.image "default" "<tag when empty>") → repo[:tag][@digest] */}}
{{- define "nokker.image" -}}
{{- $repo := .image.repository -}}
{{- $reg := .root.Values.global.imageRegistry -}}
{{- $first := first (splitList "/" $repo) -}}
{{- if and $reg (not (or (contains "." $first) (contains ":" $first))) -}}
{{- $repo = printf "%s/%s" $reg $repo -}}
{{- end -}}
{{- $tag := default .default .image.tag | toString -}}
{{- if .image.digest -}}
{{- printf "%s:%s@%s" $repo $tag .image.digest -}}
{{- else -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end -}}
{{- end -}}

{{- define "nokker.coreImage" -}}
{{- include "nokker.image" (dict "root" . "image" .Values.core.image "default" (printf "%s-lokkal" .Chart.AppVersion)) -}}
{{- end -}}

{{- define "nokker.pullSecrets" -}}
{{- with .Values.global.imagePullSecrets }}
imagePullSecrets:
{{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{/* Pod-level security context: restricted; uid/gid/fsGroup omitted when null (OpenShift assigns them). */}}
{{- define "nokker.podSecurityContext" -}}
runAsNonRoot: true
seccompProfile:
  type: RuntimeDefault
{{- with .Values.podSecurity.runAsUser }}
runAsUser: {{ . }}
{{- end }}
{{- with .Values.podSecurity.runAsGroup }}
runAsGroup: {{ . }}
{{- end }}
{{- with .Values.podSecurity.fsGroup }}
fsGroup: {{ . }}
{{- end }}
{{- end -}}

{{- define "nokker.containerSecurityContext" -}}
allowPrivilegeEscalation: false
privileged: false
runAsNonRoot: true
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{/* Secrets ---------------------------------------------------------------------------------------------- */}}
{{- define "nokker.coreSecret" -}}{{ include "nokker.fullname" . }}-core{{- end -}}
{{- define "nokker.dbSecret" -}}{{ include "nokker.fullname" . }}-db{{- end -}}
{{- define "nokker.approleSecret" -}}{{ include "nokker.fullname" . }}-openbao-approle{{- end -}}
{{- define "nokker.unsealSecret" -}}{{ include "nokker.fullname" . }}-openbao-unseal{{- end -}}

{{/* Keep a generated value across upgrades: (dict "root" . "secret" name "key" k "len" n) */}}
{{- define "nokker.keep" -}}
{{- $s := lookup "v1" "Secret" .root.Release.Namespace .secret -}}
{{- if and $s $s.data (hasKey $s.data .key) -}}
{{- index $s.data .key | b64dec -}}
{{- else -}}
{{- randAlphaNum (int .len) -}}
{{- end -}}
{{- end -}}

{{/* Database coordinates ----------------------------------------------------------------------------------- */}}
{{- define "nokker.pgHost" -}}
{{- if .Values.database.dev.enabled -}}{{ include "nokker.fullname" . }}-postgres{{- else -}}$(NOKKER_PG_HOST){{- end -}}
{{- end -}}

{{- define "nokker.workspacesClaim" -}}
{{- default (printf "%s-workspaces" (include "nokker.fullname" .)) .Values.code.workspaces.existingClaim -}}
{{- end -}}

{{- define "nokker.egressUrl" -}}
http://{{ include "nokker.fullname" . }}-egress:3128
{{- end -}}

{{- define "nokker.openbaoAddr" -}}
http://{{ include "nokker.fullname" . }}-openbao:8200
{{- end -}}

{{/* Same, for a Fernet key (32 random bytes, URL-safe base64): (dict "root" . "secret" name "key" k) */}}
{{- define "nokker.keepFernet" -}}
{{- $s := lookup "v1" "Secret" .root.Release.Namespace .secret -}}
{{- if and $s $s.data (hasKey $s.data .key) -}}
{{- index $s.data .key | b64dec -}}
{{- else -}}
{{- randBytes 32 | replace "+" "-" | replace "/" "_" -}}
{{- end -}}
{{- end -}}

{{/* Database coordinates (no password): $(NOKKER_PG_HOST) etc. are expanded in the DSNs that follow them. */}}
{{- define "nokker.dbEnv" -}}
{{- if .Values.database.dev.enabled -}}
- {name: NOKKER_PG_HOST, value: {{ include "nokker.fullname" . }}-postgres}
- {name: NOKKER_PG_PORT, value: "5432"}
- {name: NOKKER_PG_DATABASE, value: nokker}
- {name: NOKKER_CORTHEXIS_DATABASE, value: corthexis}
{{- else -}}
{{- $s := required "database.existingSecret (database.dev.enabled=false)" .Values.database.existingSecret -}}
{{- range list "NOKKER_PG_HOST" "NOKKER_PG_PORT" "NOKKER_PG_DATABASE" "NOKKER_CORTHEXIS_DATABASE" }}
- name: {{ . }}
  valueFrom: {secretKeyRef: {name: {{ $s }}, key: {{ . }}}}
{{- end }}
{{- end }}
{{- end -}}

{{/* One generated role password: (dict "root" . "key" "NOKKER_APP_PASSWORD") */}}
{{- define "nokker.rolePassword" -}}
- name: {{ .key }}
  valueFrom: {secretKeyRef: {name: {{ include "nokker.dbSecret" .root }}, key: {{ .key }}}}
{{- end -}}

{{/* Admin connection of the init containers (libpq variables) and the two role passwords. */}}
{{- define "nokker.adminEnv" -}}
{{ include "nokker.dbEnv" . }}
- {name: PGHOST, value: "$(NOKKER_PG_HOST)"}
- {name: PGPORT, value: "$(NOKKER_PG_PORT)"}
- {name: PGDATABASE, value: "$(NOKKER_PG_DATABASE)"}
- {name: PGSSLMODE, value: {{ .Values.database.sslmode | quote }}}
{{- if .Values.database.dev.enabled }}
- {name: PGUSER, value: postgres}
- name: PGPASSWORD
  valueFrom: {secretKeyRef: {name: {{ include "nokker.dbSecret" . }}, key: POSTGRES_PASSWORD}}
{{- else }}
- name: PGUSER
  valueFrom: {secretKeyRef: {name: {{ .Values.database.existingSecret }}, key: PG_ADMIN_USER}}
- name: PGPASSWORD
  valueFrom: {secretKeyRef: {name: {{ .Values.database.existingSecret }}, key: PG_ADMIN_PASSWORD}}
{{- end }}
{{ include "nokker.rolePassword" (dict "root" . "key" "NOKKER_OWNER_PASSWORD") }}
{{ include "nokker.rolePassword" (dict "root" . "key" "NOKKER_APP_PASSWORD") }}
{{- end -}}

{{/* NetworkPolicy egress rules shared by several components */}}
{{- define "nokker.np.dns" -}}
- to:
    - namespaceSelector: {}
      podSelector: {matchExpressions: [{key: k8s-app, operator: In, values: [kube-dns, coredns]}]}
    - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: openshift-dns}}
  ports: [{port: 53, protocol: UDP}, {port: 53, protocol: TCP}, {port: 5353, protocol: UDP}, {port: 5353, protocol: TCP}]
{{- end -}}
{{- define "nokker.np.internet" -}}
- to:
    - ipBlock:
        cidr: 0.0.0.0/0
        except: {{ toJson .Values.networkPolicy.blockedCidrs }}
{{- end -}}
