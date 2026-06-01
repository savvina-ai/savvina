FROM node:22-alpine AS build
RUN apk upgrade --no-cache && npm install -g npm@latest
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM alpine:3.21
RUN apk upgrade --no-cache && apk add --no-cache nginx \
    && mkdir -p /run/nginx /var/lib/nginx/tmp/client_body /var/lib/nginx/logs \
    && chmod -R 755 /var/log/nginx /var/lib/nginx /run/nginx \
    && chmod 755 /etc/nginx/http.d
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/http.d/default.conf
EXPOSE 8443
CMD ["nginx", "-g", "daemon off;"]
