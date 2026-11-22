# W01/W02 .NET 10 demonstration APIs. Build argument PROJECT selects LatencyApi or BurstApi.
FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
ARG PROJECT
WORKDIR /src
COPY workloads/dotnet ./
# Restore strictly from the committed packages.lock.json files (S10 supply-chain pinning), then publish.
RUN dotnet restore ${PROJECT}/${PROJECT}.csproj --locked-mode \
    && dotnet publish ${PROJECT}/${PROJECT}.csproj -c Release -o /out --no-restore

FROM mcr.microsoft.com/dotnet/aspnet:10.0
ARG PROJECT
COPY --from=build --chown=10001:10001 /out /app
WORKDIR /app
ENV ASPNETCORE_URLS=http://+:8080 DOTNET_EnableDiagnostics=0 APP_DLL=${PROJECT}.dll
USER 10001
ENTRYPOINT ["sh", "-c", "exec dotnet /app/$APP_DLL"]
