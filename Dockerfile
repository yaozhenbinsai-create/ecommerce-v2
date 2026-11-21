FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    ECOMMERCE2_DATA_DIR=/var/lib/weaving/data \
    SHOP_SECURITY_MODE=1 SHOP_PUBLIC_MODE=1 \
    SHOP_SECRET_KEY_FILE=/run/weaving-secrets/master.key \
    SHOP_TLS_CERT=/run/weaving-tls/fullchain.pem SHOP_TLS_KEY=/run/weaving-tls/privkey.pem \
    SHOP_BIND_HOST=0.0.0.0 SHOP_PORT=8443
WORKDIR /opt/weaving
COPY requirements-lock.txt ./
RUN pip install --no-cache-dir -r requirements-lock.txt \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin weaving \
    && mkdir -p /var/lib/weaving/data \
    && chown -R weaving:weaving /var/lib/weaving \
    && chmod 700 /var/lib/weaving/data
COPY --chown=weaving:weaving app ./app
COPY --chown=weaving:weaving tools ./tools
COPY --chown=weaving:weaving run.py ./
USER 10001:10001
EXPOSE 8443
CMD ["python", "run.py"]
