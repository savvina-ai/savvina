FROM node:22-alpine AS build
RUN apk upgrade --no-cache && npm install -g npm@latest
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM alpine:3.21
RUN apk upgrade --no-cache && apk add --no-cache nginx \
    && mkdir -p /run/nginx /var/lib/nginx/tmp/client_body /var/lib/nginx/logs /etc/nginx/templates \
    && chmod -R 755 /var/log/nginx /var/lib/nginx /run/nginx \
    # http.d must be writable by the arbitrary non-root UID the container runs
    # as (docker-compose sets user: ${LOCAL_UID}): the entrypoint renders
    # default.conf there. The stock root-owned default.conf must go — a 777
    # dir allows creating files but not truncating someone else's.
    && rm -f /etc/nginx/http.d/*.conf \
    && chmod 777 /etc/nginx/http.d
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod 755 /docker-entrypoint.sh
EXPOSE 8080
ENTRYPOINT ["/docker-entrypoint.sh"]
